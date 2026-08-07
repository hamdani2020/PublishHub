# Design Document

## Overview

PublishHub is built as three thin application services around a swappable job queue, wrapped in a GitOps-managed platform layer. The design goal is that **the same artifacts run locally and in AWS**, with the only difference being values and configuration. Everything the cluster runs comes from Git via ArgoCD; nothing is applied by hand outside the bootstrap step.

Two decisions shape the rest of the design:

1. **The queue is an interface, not a dependency.** The API and worker both talk to a `QueueClient` abstraction with `redis` and `sqs` implementations. Local development uses a Redis list (free, no AWS account); AWS uses SQS with a redrive policy. Business logic never branches on backend.
2. **Post state lives in Redis in both environments.** Only the *queue* swaps between environments. This keeps the system to one stateful dependency instead of introducing DynamoDB or RDS, at the cost of durability that a real product would need. This tradeoff is documented rather than hidden.

Reference: `#[[file:EXECUTION_GUIDE_PublishHub.md]]`

## Architecture

### Request flow

```
Browser
  │  POST /api/v1/publish
  ▼
┌──────────────────┐   1. validate
│  Web (nginx)     │   2. write post record → Redis hash + recent index
│  React SPA       │   3. enqueue job → QueueClient
└────────┬─────────┘   4. respond 202 { id, status: queued }
         │ /api proxy
         ▼
┌──────────────────┐         ┌───────────────────────────┐
│  API (Node 20)   │────────▶│  Queue                    │
│  Express + TS    │         │  local: Redis list        │
└────────┬─────────┘         │  aws:   SQS + DLQ         │
         │                   └─────────┬─────────────────┘
         │ state                       │ depth metric
         ▼                             ▼
┌──────────────────┐         ┌───────────────────────────┐
│  Redis           │         │  KEDA ScaledObject        │
│  post records    │         │  redis-list | aws-sqs     │
└────────▲─────────┘         └─────────┬─────────────────┘
         │ status update               │ scales 0..N
         │                             ▼
         │                   ┌───────────────────────────┐
         └───────────────────│  Worker (Python 3.11)     │
                             │  claim → simulate → ack   │
                             └───────────────────────────┘
```

### Platform layer

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

ArgoCD, KEDA, and Argo Rollouts themselves are installed by `make platform-install` (a one-time bootstrap, since something must install the thing that installs everything). From that point forward ArgoCD owns the cluster.

### Environment matrix

| Concern | Local (kind) | AWS (EKS) |
|---|---|---|
| Cluster | kind, 1 control plane + 2 workers | EKS 1.29, managed node groups, Spot + ARM |
| Registry | local registry `localhost:5001` | ECR per service |
| Queue | Redis list | SQS + DLQ (redrive) |
| KEDA scaler | `redis` | `aws-sqs-queue` via IRSA |
| Post state | in-cluster Redis | in-cluster Redis (documented tradeoff) |
| Observability | stdout JSON logs only | Datadog agent, APM, custom metrics |
| Secrets | none required | Kubernetes Secrets, IRSA, GitHub OIDC |

## Components and Interfaces

### 1. Message envelope (the contract between services)

A single versioned schema, shared by both languages and both queue backends. Documented in `docs/message-schema.md` and mirrored by a TypeScript type and a Python dataclass.

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

`trace_context` carries Datadog distributed-trace headers so the worker span links to the originating API request. When tracing is disabled the field is an empty object and the worker starts a root span.

### 2. Queue abstraction

Both languages implement the same four operations. Nothing above this layer knows which backend is active.

```ts
interface QueueClient {
  enqueue(job: PublishJob): Promise<void>;
  receive(waitSeconds: number): Promise<ReceivedJob | null>;
  ack(job: ReceivedJob): Promise<void>;
  deadLetter(job: ReceivedJob, reason: string): Promise<void>;
  depth(): Promise<number>;
  close(): Promise<void>;
}
```

| Operation | Redis implementation | SQS implementation |
|---|---|---|
| `enqueue` | `LPUSH publishhub:jobs` | `SendMessage` |
| `receive` | `BRPOPLPUSH jobs → processing` (reliable queue pattern) | `ReceiveMessage` with long polling |
| `ack` | `LREM processing` | `DeleteMessage` |
| `deadLetter` | `LPUSH publishhub:jobs:dlq` + `LREM processing` | rely on redrive policy, or explicit send to DLQ |
| `depth` | `LLEN publishhub:jobs` | `ApproximateNumberOfMessages` |

Redis uses `BRPOPLPUSH` rather than `BRPOP` so a worker killed mid-job leaves the message in a `processing` list instead of losing it. A reaper on worker startup returns stale `processing` entries older than the visibility window back to the main queue.

### 3. API service — `apps/api`

Node 20, TypeScript, Express 4, `zod` for validation, `pino` for structured logging, `dd-trace` loaded conditionally.

| Endpoint | Method | Behavior |
|---|---|---|
| `/health` | GET | Liveness. Process-only, never touches Redis. Always fast. |
| `/ready` | GET | Readiness. `PING` Redis and check queue reachable. 503 on failure. |
| `/api/v1/publish` | POST | Validate → persist post record → enqueue → `202 { id, status }` |
| `/api/v1/posts` | GET | Recent posts from Redis index, newest first, limit-capped |
| `/api/v1/posts/:id` | GET | Single post record, 404 when unknown |
| `/metrics` | GET | Prometheus-format counters (also useful without Datadog) |

Validation rules: `content` is a non-empty string up to 5000 characters; `platforms` is a non-empty array whose members are in the configured allow-list (`twitter`, `linkedin`, `mastodon`, `bluesky`); unknown body fields are stripped.

Error envelope, used for every non-2xx response:

```json
{ "error": { "code": "VALIDATION_FAILED", "message": "content must not be empty", "request_id": "..." } }
```

Security posture: no end-user auth (documented in Requirement 2.9). CORS reads an allow-list from `CORS_ORIGINS`; the value `*` is permitted only when `NODE_ENV=development`. `helmet` sets default security headers. Request bodies are size-limited. The API is a `ClusterIP` Service only, reached via port-forward locally; no public Ingress is created by default.

### 4. Worker service — `apps/worker`

Python 3.11, `redis-py`, `boto3`, `ddtrace` conditionally, `tenacity` for backoff.

Main loop:

1. Reap stale `processing` entries (Redis backend only).
2. `receive(wait_seconds=20)`. On `None`, loop — blocking receive means no CPU spin.
3. Extract `trace_context`, start a child span.
4. For each platform, simulate a publish with a configurable latency and failure probability (`SIMULATE_FAILURE_RATE`, default 0) so canary and retry paths are demonstrable.
5. On success: write terminal status to Redis, `ack`.
6. On failure: increment `attempt`, re-enqueue with exponential backoff up to `MAX_ATTEMPTS` (default 3), then `deadLetter`.
7. Emit structured log and metrics for every outcome.

Graceful shutdown is the crux of safe scale-to-zero. A `SIGTERM` handler sets a stop flag; the loop finishes the current job, acks it, closes connections, and exits. `terminationGracePeriodSeconds` in the chart exceeds the worst-case single-job duration, so KEDA scaling down never loses a message.

### 5. Web frontend — `apps/web`

React 18, Vite, TypeScript. Served by `nginxinc/nginx-unprivileged` listening on 8080 (a non-root container cannot bind port 80).

Runtime configuration, not build-time: the container entrypoint writes `/usr/share/nginx/html/config.js` from environment variables, and the app reads `window.__PUBLISHHUB_CONFIG__`. One image, every environment. nginx also proxies `/api` to the API Service so the browser makes same-origin requests and CORS is a non-issue in normal operation.

Accessibility is designed in, not retrofitted: the composer is a real `<form>` with `<label>` elements bound to inputs, platforms are checkboxes in a `<fieldset>` with a `<legend>`, submit results are announced through an `aria-live="polite"` region, the pending state uses `aria-busy` and a disabled button with visible text change, error messages are tied to inputs via `aria-describedby`, and focus is moved to the result region after submission. Colors are chosen for WCAG AA contrast. Note that full WCAG validation requires manual testing with a screen reader and expert review; the implementation targets the checkable subset.

### 6. Helm chart — `helm/publishhub`

```
helm/publishhub/
├── Chart.yaml
├── values.yaml                  # local/kind defaults
├── values-production.yaml       # EKS: ECR images, SQS, Datadog on
├── templates/
│   ├── _helpers.tpl             # names, labels, selectors, image refs
│   ├── configmap.yaml           # non-secret app config
│   ├── api-deployment.yaml      # rendered when api.rollout.enabled=false
│   ├── api-rollout.yaml         # rendered when api.rollout.enabled=true
│   ├── api-service.yaml         # + canary/stable services for rollouts
│   ├── api-hpa.yaml
│   ├── worker-deployment.yaml
│   ├── worker-keda.yaml         # scaler type follows queue.backend
│   ├── web-deployment.yaml
│   ├── web-service.yaml
│   ├── redis.yaml
│   └── serviceaccount.yaml      # IRSA annotation in production
└── tests/                       # helm unittest cases
```

Two details worth flagging. First, `api-deployment.yaml` and `api-rollout.yaml` are mutually exclusive via `api.rollout.enabled`, because a Deployment and a Rollout selecting the same pods would fight (Requirement 10.6). Second, when the Rollout is active the HPA's `scaleTargetRef` points at `argoproj.io/v1alpha1/Rollout`, not the Deployment.

Every workload gets: resource requests and limits, liveness and readiness probes on the real endpoints, `runAsNonRoot`, `allowPrivilegeEscalation: false`, dropped capabilities, and a `terminationGracePeriodSeconds` matched to its shutdown behavior. Required-but-undefaultable values use `{{ required "message" .Values.x }}`.

### 7. ArgoCD configuration — `argocd/`

App of Apps. `bootstrap.yaml` is the single manifest applied by hand; it points at `argocd/applications/` and everything else follows.

The repository URL appears in exactly one place — a `repoURL` value threaded through the Application manifests — so forking the project means editing one line rather than five files (Requirement 8.7).

The `publishhub` AppProject restricts source repositories to this repo, destinations to the `publishhub` namespace, and cluster-scoped resource kinds to an explicit allow-list. Applications enable `automated` sync with `prune: true` and `selfHeal: true`, which is what makes manual `kubectl edit` self-correcting.

### 8. KEDA ScaledObject

```yaml
minReplicaCount: 0
maxReplicaCount: 10
pollingInterval: 15
cooldownPeriod: 60
triggers:
  # queue.backend == redis
  - type: redis
    metadata: { addressFromEnv: REDIS_ADDRESS, listName: "publishhub:jobs", listLength: "10" }
  # queue.backend == sqs
  - type: aws-sqs-queue
    metadata: { queueURL: ..., queueLength: "10", awsRegion: ... }
    authenticationRef: { name: keda-aws-irsa }
```

`listLength: 10` means KEDA targets 10 pending messages per replica: 50 queued messages drive roughly 5 replicas. Scale-to-zero is what makes the cost story real, and it only works because the worker shuts down gracefully.

### 9. Argo Rollouts canary

Six steps, matching the guide's expected output:

| Step | Action |
|---|---|
| 1 | `setWeight: 10` |
| 2 | `pause: {}` — indefinite, requires manual promote |
| 3 | `setWeight: 25` |
| 4 | `pause: { duration: 30s }` |
| 5 | `setWeight: 50` |
| 6 | `pause: { duration: 30s }` → then full promotion |

An honest limitation: without a service mesh or an ingress traffic-router, Argo Rollouts approximates traffic weight through replica ratios rather than splitting requests precisely. This is fine for the local demo and is documented; the production path is to add a traffic provider. An `AnalysisTemplate` querying Datadog error rate is wired as an optional step so auto-abort (Requirement 10.4) has a real trigger where Datadog is configured, with a `job`-based fallback probe for local use.

### 10. Developer CLI — `cli/publishctl`

Python, `click` for command structure, `rich` for output. The CLI is a thin, honest wrapper: it shells out to `make`, `kubectl`, and `helm` and surfaces their exit codes and stderr rather than swallowing them.

```
publishctl env start|stop|status
publishctl status
publishctl logs --service api|worker|web [--tail] [--follow]
publishctl publish --content TEXT --platforms twitter,linkedin
publishctl scale --replicas N
publishctl rollout status|promote|abort
publishctl incident --pod NAME [--namespace NS]
publishctl doctor            # verify prerequisites
```

`doctor` exists because most first-run failures are a missing tool, and a clear "kind is not installed, run brew install kind" beats a stack trace (Requirement 1.6). Subprocess calls pass argument lists, never interpolated shell strings, so user-supplied values cannot inject commands.

### 11. AI incident analyzer — `scripts/ai-incident-analyzer.py`

Structure follows the guide: collect with `kubectl`, analyze with Bedrock, print. The design adds three things the guide's draft omits.

**Redaction before transmission.** Pod descriptions and logs routinely contain environment variables, tokens, and connection strings. A redaction pass replaces matches for common secret patterns (`AKIA[0-9A-Z]{16}`, bearer tokens, `password=`, `*_KEY=`, `*_SECRET=`, JWT-shaped strings, URLs with embedded credentials) with `[REDACTED]` before anything leaves the machine (Requirement 12.6).

**Bounded input.** Each section is truncated to a documented budget — description 3000, logs 4000, previous logs 2000, events 2000 characters — keeping a single analysis at a predictable and small token cost.

**Actionable failure messages.** Bedrock errors are mapped to causes rather than surfaced raw:

| Error | Message |
|---|---|
| `AccessDeniedException` | Model access not granted; enable Claude 3 Haiku in the Bedrock console for this region |
| `ValidationException` on model id | Model unavailable in region; try `us-east-1` |
| `ExpiredTokenException` / `NoCredentialsError` | AWS credentials missing or expired; run `aws configure` or refresh SSO |
| `ThrottlingException` | Retry with backoff |

Credentials come from the ambient AWS chain only. No API key is read from or written to the repository.

### 12. Terraform — `terraform/`

```
terraform/
├── main.tf, variables.tf, outputs.tf, versions.tf
├── backend.tf.example        # S3 + DynamoDB locking, opt-in
└── modules/
    ├── vpc/      3 AZs, public + private subnets, single NAT (cost)
    ├── eks/      1.29, managed node groups, Spot, ARM (t4g/m7g), IRSA/OIDC
    ├── sqs/      main queue + DLQ with redrive
    ├── ecr/      one repo per service + lifecycle policy
    └── iam/      IRSA roles: KEDA SQS read, worker SQS consume, GitHub OIDC
```

Cost controls are defaults, not options: Spot capacity, ARM instance types, a single NAT gateway, and an ECR lifecycle policy expiring untagged images after 1 day and keeping the last 30 tagged images.

Terraform is deliberately **not** wired into any `make` target that could apply or destroy without a prompt (Requirement 13.8). `terraform init` and `validate` are automatable; `apply` and `destroy` stay manual, interactive, and documented with their cost implications. `terraform.tfstate*`, `*.tfvars`, and `.terraform/` are gitignored.

### 13. Observability — `observability/datadog/`

Everything routes through a single `OBSERVABILITY_ENABLED` switch, default off, so the local path needs no Datadog account (Requirement 14.6). When off, tracer initialization is skipped and the metrics client becomes a no-op recorder that still increments the Prometheus counters on `/metrics`.

Custom metrics: `publishhub.posts.submitted`, `publishhub.jobs.processed`, `publishhub.jobs.failed`, `publishhub.jobs.duration`, `publishhub.queue.depth`, tagged by platform, status, and environment.

Monitors stored as code in `monitors.yaml`: API 5xx rate above 1% over 5 minutes, worker failure rate above 5%, queue depth growing for 10 minutes, any pod restarting more than 3 times in 15 minutes, worker stuck at zero replicas while queue is non-empty.

### 14. CI/CD — `.github/workflows/`

| Workflow | Trigger | Jobs |
|---|---|---|
| `ci.yaml` | PR, push | lint + typecheck + unit tests per service; `helm lint` and `template` for each values file; `terraform fmt -check` and `validate`; build images without push; Trivy scan failing on CRITICAL |
| `deploy.yaml` | push to main, tags | OIDC assume role → build and push to ECR tagged with SHA → `yq` update Helm values → commit back |

Two failure modes handled explicitly. The commit-back step appends `[skip ci]` and the workflow ignores pushes from its own bot identity, preventing an infinite loop (Requirement 15.5). And a preflight step asserts every required secret and variable is present, failing with the missing name instead of an unhelpful error deep in the run (Requirement 15.7).

## Configuration Reference

| Variable | Service | Default (local) | Purpose |
|---|---|---|---|
| `PORT` | api | `8080` | Listen port |
| `NODE_ENV` | api | `development` | Enables permissive CORS only here |
| `CORS_ORIGINS` | api | `http://localhost:3000` | Comma-separated allow-list |
| `QUEUE_BACKEND` | api, worker | `redis` | `redis` \| `sqs` |
| `REDIS_URL` | api, worker | `redis://publishhub-redis:6379` | State store and local queue |
| `SQS_QUEUE_URL` | api, worker | unset | Required when backend is `sqs` |
| `AWS_REGION` | api, worker | `us-east-1` | SQS and Bedrock region |
| `MAX_ATTEMPTS` | worker | `3` | Retries before dead-letter |
| `POLL_WAIT_SECONDS` | worker | `20` | Blocking receive window |
| `SIMULATE_LATENCY_MS` | worker | `500` | Per-platform simulated work |
| `SIMULATE_FAILURE_RATE` | worker | `0` | 0.0–1.0, for exercising retries |
| `OBSERVABILITY_ENABLED` | all | `false` | Datadog tracing and metrics |
| `DD_ENV`, `DD_SERVICE`, `DD_VERSION` | all | — | Datadog unified tagging |
| `API_BASE_URL` | web | `/api` | Injected at container start |

Startup validation fails fast with the offending key name when a required value for the selected backend is missing (Requirement 5.5).

## Error Handling

| Failure | Behavior |
|---|---|
| Invalid publish payload | `400` with error code, no enqueue |
| Redis unreachable at API startup | Retry with backoff, `/ready` returns 503, `/health` stays 200 so Kubernetes does not kill a pod waiting on a dependency |
| Queue unreachable during enqueue | `503 QUEUE_UNAVAILABLE`, logged with correlation id, no partial post record |
| Worker job raises | Retry with exponential backoff to `MAX_ATTEMPTS`, then dead-letter |
| Worker killed mid-job | Message remains in `processing`; reaper returns it after the visibility window |
| Poison message (unparseable) | Dead-letter immediately, do not retry, log the raw payload truncated |
| Unknown `schema_version` | Dead-letter with an explicit reason rather than guessing the shape |
| ArgoCD sync failure | Application reports `Degraded`; previous healthy state remains running |
| Canary failing | Auto-abort on analysis failure, or manual `rollout abort`; stable version takes all traffic |
| Bedrock denied or throttled | Mapped diagnostic message, non-zero exit |

The recurring principle: liveness reflects the process, readiness reflects dependencies. Conflating them causes cascading restarts when a dependency blips.

## Testing Strategy

| Layer | Scope | Tooling |
|---|---|---|
| Unit — API | Validation rules, error envelope, queue client with a fake backend, graceful shutdown | Vitest |
| Unit — Worker | Retry and backoff, dead-letter on exhaustion, poison message handling, SIGTERM mid-job | pytest |
| Unit — Web | Composer submit, error display, pending state, live-region announcement | Vitest + Testing Library |
| Contract | One message-schema fixture asserted by both the TS and Python test suites, so the two implementations cannot drift | shared JSON fixture |
| Chart | `helm lint` + `helm template` per values file; assertions that Deployment and Rollout are mutually exclusive and that probes and limits exist on every workload | helm unittest |
| Integration | API + Redis + worker via docker compose: submit a post, assert it reaches terminal status | pytest + compose |
| Platform | Post-deploy smoke script: all pods ready, ScaledObject ready, `/health` 200, one post reaches terminal status | shell + kubectl |
| Scaling | Burst 50 posts, assert worker replicas exceed 1 and return to minimum | shell + kubectl |
| Infrastructure | `terraform fmt -check`, `validate`, and plan review. No automated apply. | Terraform CLI |

Per the project's operating rules, tests are written for the behaviors above as part of implementation, not bolted on afterward.

## Security Considerations

- **No end-user authentication.** The API is unauthenticated by design as an internal gateway. It is exposed only as `ClusterIP` with port-forward access; no Ingress or LoadBalancer is created by default. This is stated in the README rather than left for a reader to discover.
- **No long-lived AWS credentials anywhere.** IRSA for in-cluster workloads, OIDC for GitHub Actions, the ambient credential chain for the Bedrock script.
- **Secrets never in Git.** The chart references Secrets by name. `.gitignore` covers `.env*`, `*.tfvars`, `terraform.tfstate*`, and kubeconfigs. Documentation uses placeholder values and avoids putting real keys in shell examples that land in history.
- **Redaction before AI transmission.** Pod logs and descriptions are scrubbed of credential-shaped strings before being sent to Bedrock.
- **Container hardening.** Non-root users, no privilege escalation, dropped capabilities, pinned base images, multi-stage builds that exclude build toolchains.
- **Supply chain.** Pinned dependency versions, Trivy scanning in CI failing on critical findings.
- **Injection safety.** CLI and scripts invoke subprocesses with argument lists, never string-interpolated shells.

## Repository Layout

```
publishhub/
├── apps/{api,worker,web}/
├── helm/publishhub/
├── argocd/{bootstrap.yaml,applications,projects,rollouts}/
├── terraform/{main.tf,variables.tf,outputs.tf,modules/}
├── cli/publishctl/
├── scripts/{kind-with-registry.sh,ai-incident-analyzer.py,smoke-test.sh,load-test.sh}
├── observability/datadog/{values.yaml,monitors.yaml,dashboard.json}
├── docs/{architecture.md,local-dev.md,message-schema.md,runbooks/}
├── .github/workflows/{ci.yaml,deploy.yaml}
├── docker-compose.yaml          # fast inner loop without Kubernetes
├── Makefile
├── .gitignore
└── README.md
```

## Design Decisions and Tradeoffs

| Decision | Rationale | Tradeoff accepted |
|---|---|---|
| Queue abstraction over direct SQS | Local development stays free and offline | An extra indirection layer in two languages |
| Redis for post state in both environments | One stateful dependency instead of adding DynamoDB | Not durable enough for a real product; documented |
| App of Apps in ArgoCD | Bootstrap is one manifest; adding an app is a file, not a procedure | Indirection when debugging a sync failure |
| Rollouts without a service mesh | Keeps the local stack small | Traffic weights are approximated by replica ratio |
| Claude 3 Haiku for incident analysis | Fast and inexpensive; adequate for log triage | Less depth than a larger model on subtle failures |
| Spot + ARM by default | Materially lower AWS cost | Spot interruptions; images must be multi-arch |
| Terraform apply left manual | Prevents accidental spend and destruction | One more manual step in the AWS path |
| `docker-compose.yaml` alongside Kubernetes | Seconds-long inner loop for app code | A second environment definition to keep honest |
