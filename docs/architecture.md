# Architecture

This document describes PublishHub's system architecture, component interactions, platform layer, and the design decisions that shaped the implementation.

## System Overview

PublishHub is three thin application services around a swappable job queue, wrapped in a GitOps-managed platform layer. The design goal is that **the same artifacts run locally and in AWS**, with the only difference being values and configuration.

Two decisions shape the rest of the architecture:

1. **The queue is an interface, not a dependency.** The API and worker both talk to a `QueueClient` abstraction with `redis` and `sqs` implementations. Local development uses a Redis list (free, no AWS account); AWS uses SQS with a redrive policy. Business logic never branches on backend.
2. **Post state lives in Redis in both environments.** Only the queue swaps between environments. This keeps the system to one stateful dependency instead of introducing DynamoDB or RDS, at the cost of durability that a real product would need.

## Request Flow

![architecture](/assets/build-architecture.jpg)

### Lifecycle of a publish request

1. The browser submits to `POST /api/v1/publish` through the nginx reverse proxy.
2. The API validates the payload with zod (`content` non-empty, max 5000 chars; `platforms` non-empty, members in allow-list).
3. A post record is persisted to a Redis hash and added to the recent-posts index.
4. A versioned job message is enqueued via `QueueClient.enqueue()`.
5. The API responds `202 Accepted` with the post ID and status `queued`.
6. KEDA polls queue depth and adjusts worker replica count.
7. A worker instance calls `QueueClient.receive()` with a blocking wait.
8. The worker simulates publishing for each platform, updates the post status in Redis, and acknowledges the message.
9. On failure, the worker retries with exponential backoff up to `MAX_ATTEMPTS`, then dead-letters.

## Components

### API Service (`apps/api`)

Node 20, TypeScript, Express 4.

| Endpoint | Method | Behavior |
|---|---|---|
| `/health` | GET | Liveness — process-only, never touches Redis |
| `/ready` | GET | Readiness — pings Redis and queue, 503 on failure |
| `/api/v1/publish` | POST | Validate → persist → enqueue → 202 |
| `/api/v1/posts` | GET | Recent posts, newest first, limit-capped |
| `/api/v1/posts/:id` | GET | Single post record, 404 when unknown |
| `/metrics` | GET | Prometheus-format counters |

Key libraries: `zod` (validation), `pino` (structured JSON logging), `dd-trace` (loaded conditionally when `OBSERVABILITY_ENABLED=true`), `helmet` (security headers), `ioredis` (Redis client).

**Security posture:** No end-user authentication (by design). CORS reads an allow-list from `CORS_ORIGINS`; the wildcard `*` is permitted only when `NODE_ENV=development`. Request bodies are size-limited. The service is `ClusterIP` only — no public Ingress is created.

**Graceful shutdown:** On `SIGTERM`, stops accepting connections, drains in-flight requests, closes Redis and queue clients, exits within the grace period.

### Worker Service (`apps/worker`)

Python 3.11.

Main loop:

1. Reap stale `processing` entries (Redis backend only — handles workers killed mid-job).
2. `receive(wait_seconds=20)`. On `None`, loop — blocking receive means no CPU spin.
3. Extract `trace_context`, start a child span (when tracing is active).
4. For each platform, simulate publishing with configurable latency and failure probability.
5. On success: write terminal status to Redis, acknowledge the message.
6. On failure: increment `attempt`, re-enqueue with exponential backoff up to `MAX_ATTEMPTS`, then dead-letter.
7. Emit structured log and metrics for every outcome.

**Graceful shutdown:** `SIGTERM` sets a stop flag; the loop finishes the current job, acks it, closes connections, exits. `terminationGracePeriodSeconds` exceeds worst-case job duration so KEDA scale-down never loses a message.

Key libraries: `redis-py`, `boto3`, `ddtrace` (conditional), `tenacity` (backoff).

### Web Frontend (`apps/web`)

React 18, Vite, TypeScript. Served by `nginxinc/nginx-unprivileged` on port 8080.

**Runtime configuration:** The container entrypoint writes `/usr/share/nginx/html/config.js` from environment variables. The app reads `window.__PUBLISHHUB_CONFIG__`. One image, every environment. nginx proxies `/api` to the API service (same-origin requests, no CORS issues in normal operation).

**Accessibility:** Real `<form>` with `<label>` elements, checkboxes in a `<fieldset>` with `<legend>`, `aria-live="polite"` for status announcements, `aria-busy` during submission, `aria-describedby` for error messages, WCAG AA contrast ratios.

### Queue Abstraction

Both languages implement four operations through a shared interface:

```
enqueue(job)      — push a job to the queue
receive(wait)     — blocking/long-poll receive
ack(job)          — acknowledge successful processing
deadLetter(job)   — move to DLQ after exhausting retries
depth()           — current queue depth (for metrics)
close()           — clean shutdown
```

| Operation | Redis | SQS |
|---|---|---|
| `enqueue` | `LPUSH publishhub:jobs` | `SendMessage` |
| `receive` | `BRPOPLPUSH jobs → processing` | `ReceiveMessage` (long poll) |
| `ack` | `LREM processing` | `DeleteMessage` |
| `deadLetter` | `LPUSH publishhub:jobs:dlq` | Redrive policy / explicit send |
| `depth` | `LLEN publishhub:jobs` | `ApproximateNumberOfMessages` |

Redis uses `BRPOPLPUSH` (the reliable queue pattern) so a worker killed mid-job leaves the message in a `processing` list rather than losing it.

### Message Envelope

A versioned schema shared by both languages and both queue backends:

```json
{
  "schema_version": 1,
  "job_id": "uuid-v4",
  "post_id": "post_01H...",
  "content": "string",
  "platforms": ["twitter", "linkedin"],
  "attempt": 1,
  "enqueued_at": "2026-08-07T10:00:00.000Z",
  "trace_context": { "x-datadog-trace-id": "...", "x-datadog-parent-id": "..." }
}
```

Full specification: [docs/message-schema.md](message-schema.md).

## Platform Layer

### ArgoCD — GitOps Delivery

```
Git repository (source of truth)
  │
  ▼
argocd/bootstrap.yaml  ──App of Apps──▶  argocd/applications/*.yaml
                                            │
                    ┌───────────────────────┼──────────────────────┐
                    ▼                       ▼                      ▼
             helm/publishhub          argocd/rollouts        observability/
             (api, worker, web,       (api canary)           (Datadog config)
              redis, ScaledObject)
```

`bootstrap.yaml` is the single manifest applied by hand (`make argocd-sync`). Everything else syncs automatically with `prune: true` and `selfHeal: true` — manual `kubectl edit` is self-correcting.

The `publishhub` AppProject restricts:
- Source repositories to this repo
- Destinations to the `publishhub` namespace
- Cluster-scoped resources to an explicit allow-list

Repository URL appears in one configurable location, so forking requires editing one line.

### KEDA — Event-Driven Autoscaling

```yaml
minReplicaCount: 0
maxReplicaCount: 10
pollingInterval: 15      # seconds between depth checks
cooldownPeriod: 60       # seconds at zero depth before scale-to-zero
triggers:
  - type: redis          # or aws-sqs-queue
    metadata:
      listName: "publishhub:jobs"
      listLength: "10"   # target messages per replica
```

`listLength: 10` means 50 queued messages drive roughly 5 replicas. Scale-to-zero works because the worker shuts down gracefully — KEDA can safely terminate idle workers.

### Argo Rollouts — Progressive Delivery

Canary strategy with six steps:

| Step | Action |
|---|---|
| 1 | `setWeight: 10` |
| 2 | `pause: {}` — indefinite, requires manual promote |
| 3 | `setWeight: 25` |
| 4 | `pause: { duration: 30s }` |
| 5 | `setWeight: 50` |
| 6 | `pause: { duration: 30s }` → full promotion |

An `AnalysisTemplate` can query Datadog error rate for auto-abort, with a job-based fallback probe for local use.

**Limitation:** Without a service mesh, Argo Rollouts approximates traffic weight through replica ratios rather than splitting requests precisely. This is documented and fine for the local demo.

The Helm chart renders either a Deployment or a Rollout (never both), controlled by `api.rollout.enabled`, because two controllers selecting the same pods would fight.

## Environment Matrix

| Concern | Local (kind) | AWS (EKS) |
|---|---|---|
| Cluster | kind, 1 control plane + 2 workers | EKS 1.29, managed node groups, Spot + ARM |
| Registry | local `localhost:5001` | ECR per service |
| Queue | Redis list | SQS + DLQ (redrive policy) |
| KEDA scaler | `redis` type | `aws-sqs-queue` via IRSA |
| Post state | in-cluster Redis | in-cluster Redis |
| Observability | stdout JSON logs only | Datadog agent, APM, custom metrics |
| Secrets | none required | Kubernetes Secrets, IRSA, GitHub OIDC |
| Delivery | ArgoCD + local images | ArgoCD + ECR images |

The same Helm chart serves both environments with different `values.yaml` files.

## Infrastructure (AWS Path)

```
terraform/
├── main.tf, variables.tf, outputs.tf, versions.tf
├── backend.tf.example        # S3 + DynamoDB locking (opt-in)
└── modules/
    ├── vpc/      3 AZs, public + private subnets, single NAT (cost)
    ├── eks/      1.29, managed node groups, Spot, ARM (t4g/m7g), IRSA/OIDC
    ├── sqs/      main queue + DLQ with redrive
    ├── ecr/      one repo per service + lifecycle policy
    └── iam/      IRSA roles: KEDA SQS read, worker SQS consume, GitHub OIDC
```

Cost controls are defaults: Spot capacity, ARM instance types, a single NAT gateway, ECR lifecycle policy (untagged images expire after 1 day, keep last 30 tagged).

Terraform is deliberately not wired into any `make` target that applies or destroys. Read-only targets (`make tf-init`, `make tf-fmt`, `make tf-validate`, `make tf-plan`) are safe. The apply/destroy procedure is manual and documented in `docs/runbooks/aws-deployment.md`.

## CI/CD

| Workflow | Trigger | Jobs |
|---|---|---|
| `ci.yaml` | PR, push | Lint + typecheck + tests per service; Helm lint; Terraform validate; build without push; Trivy scan |
| `deploy.yaml` | push to main | OIDC → build + push to ECR (SHA tag) → update Helm values → commit back |

Deploy avoids infinite loops: the commit-back step appends `[skip ci]` and the workflow ignores pushes from its own bot identity.

## Observability

Everything routes through `OBSERVABILITY_ENABLED` (default: `false`). When off, tracing is skipped and the metrics client becomes a no-op — local development needs no Datadog account.

Custom metrics:
- `publishhub.posts.submitted`
- `publishhub.jobs.processed`
- `publishhub.jobs.failed`
- `publishhub.jobs.duration`
- `publishhub.queue.depth`

Monitors (stored as code in `observability/datadog/monitors.yaml`):
- API 5xx rate > 1% over 5 minutes
- Worker failure rate > 5%
- Queue depth growing for 10 minutes
- Pod restarting > 3 times in 15 minutes
- Worker at zero replicas while queue is non-empty

Distributed tracing propagates context through the message envelope, correlating the API request span with the worker's processing span.

## Error Handling

| Failure | Behavior |
|---|---|
| Invalid publish payload | 400 with error code, no enqueue |
| Redis unreachable at API startup | Retry with backoff, `/ready` → 503, `/health` stays 200 |
| Queue unreachable during enqueue | 503 `QUEUE_UNAVAILABLE`, logged with correlation id |
| Worker job raises | Retry with exponential backoff to `MAX_ATTEMPTS`, then dead-letter |
| Worker killed mid-job | Message remains in `processing`; reaper returns it on next startup |
| Poison message (unparseable) | Dead-letter immediately, no retry |
| Unknown `schema_version` | Dead-letter with explicit reason |
| ArgoCD sync failure | `Degraded` status; previous healthy state remains running |
| Canary failing | Auto-abort on analysis failure; stable version takes all traffic |

**Principle:** Liveness reflects the process, readiness reflects dependencies. Conflating them causes cascading restarts when a dependency blips.

## Design Decisions and Tradeoffs

| Decision | Rationale | Tradeoff |
|---|---|---|
| Queue abstraction over direct SQS | Local development stays free and offline | Extra indirection in two languages |
| Redis for post state in both envs | One stateful dependency, not two | Not durable enough for production; documented |
| App of Apps in ArgoCD | Bootstrap is one manifest; adding an app is a file | Indirection when debugging sync failures |
| Rollouts without a service mesh | Local stack stays small | Traffic weights approximate via replica ratio |
| Helm (not Kustomize) | Templating with values per environment | More complex template syntax |
| Docker Compose alongside Kubernetes | Seconds-long inner loop for app code | A second environment definition to keep honest |
| Spot + ARM by default | Materially lower AWS cost | Spot interruptions; images must be multi-arch |
| Terraform apply left manual | Prevents accidental spend and destruction | One more manual step in the AWS path |
| No authentication | Internal-facing gateway, out of scope | Must document and restrict network exposure |
| Scale-to-zero workers | Zero cost when idle | Cold start on first message after cooldown |
| Claude 3 Haiku for incidents | Fast and inexpensive for log triage | Less depth than larger models on subtle failures |

## Security Considerations

- **No end-user auth.** The API is unauthenticated by design, exposed only as ClusterIP. This is explicit, not hidden.
- **No long-lived AWS credentials.** IRSA for in-cluster workloads, OIDC for GitHub Actions, ambient chain for Bedrock.
- **Secrets never in Git.** Chart references Secrets by name. `.gitignore` covers `.env*`, `*.tfvars`, `terraform.tfstate*`, kubeconfigs.
- **Redaction before AI.** Pod logs are scrubbed of credential patterns before Bedrock transmission.
- **Container hardening.** Non-root, no privilege escalation, dropped capabilities, pinned base images, multi-stage builds.
- **CORS restriction.** Allow-list from `CORS_ORIGINS`; wildcard only in development mode.
- **Injection safety.** CLI and scripts use argument lists, never string-interpolated shells.
