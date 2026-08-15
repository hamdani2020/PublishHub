# PublishHub

A production-like developer platform demonstrating a modern publishing stack: three application services running on Kubernetes, managed by GitOps, with event-driven autoscaling, progressive delivery, infrastructure-as-code, and observability — all runnable locally at zero cost.

## Architecture

![build-architectue](/assets/build-architecture.jpg)

The platform layer wraps these services with ArgoCD (GitOps delivery), KEDA (queue-driven autoscaling including scale-to-zero), and Argo Rollouts (canary deployments). Terraform provisions the AWS equivalent when needed.

## Technology Choices

| Layer | Technology | Rationale |
|---|---|---|
| API | Node 20, Express, TypeScript, Zod | Fast request handling, strong typing, declarative validation |
| Worker | Python 3.11, redis-py, boto3 | Best-in-class AWS SDK, clear async patterns for job processing |
| Frontend | React 18, Vite, TypeScript | Fast HMR inner loop, type-safe component model |
| Queue | Redis list (local) / SQS (AWS) | Zero-cost local dev; managed, durable queue in production |
| State | Redis | Single stateful dependency for both environments |
| Packaging | Helm | One chart, multiple value files per environment |
| GitOps | ArgoCD (App of Apps) | Git as single source of truth, self-healing drift correction |
| Scaling | KEDA | Scale-to-zero on empty queue, burst absorption |
| Progressive delivery | Argo Rollouts | Canary steps with analysis-driven auto-abort |
| Infrastructure | Terraform (modular) | Reproducible AWS provisioning with cost controls |
| Observability | Datadog (opt-in) | Traces, metrics, and logs correlated by trace context |
| CI/CD | GitHub Actions | PR validation, image build, GitOps-triggered deploy |

**Key design decision:** the queue is an interface, not a dependency. A `QueueClient` abstraction with `redis` and `sqs` backends means business logic never branches on environment. Switch backends by changing one environment variable.

## Quick Start

### Prerequisites

- Docker (with Docker Compose)
- Node.js 20+
- Python 3.11+
- For the Kubernetes path: `kind`, `kubectl`, `helm`

Run `make check-tools` to verify everything is installed — missing tools are reported with install commands.

### Fast Path: Docker Compose (seconds)

The inner loop for writing application code. No cluster, no image build, no registry:

```bash
make dev-up
```

This starts Redis, the API, the worker, and the web frontend with hot-reload:

| Service | URL |
|---|---|
| Web | http://localhost:3000 |
| API | http://localhost:8080 |
| Redis | localhost:6379 |

Stop with `make dev-down`.

### Full Path: Local Kubernetes

The path that proves the deployment pipeline, GitOps, autoscaling, and canary delivery:

```bash
# 1. Create the kind cluster and local registry
make cluster-up

# 2. Build and push images to the local registry
make apps-build

# 3. Install ArgoCD, KEDA, and Argo Rollouts
make platform-install

# 4. Deploy via ArgoCD (App of Apps)
make argocd-sync

# 5. Access services through port-forward
make web-port-forward   # http://localhost:3000
make api-port-forward   # http://localhost:8081
```

View the ArgoCD dashboard:

```bash
make argocd-password     # print the admin password
make argocd-port-forward # https://localhost:8443
```

Tear everything down:

```bash
make clean
```

## Demo Flow

Once the stack is running (either path), exercise the full platform:

### 1. Submit a post

```bash
curl -X POST http://localhost:8080/api/v1/publish \
  -H 'Content-Type: application/json' \
  -d '{"content": "Hello from PublishHub!", "platforms": ["twitter", "linkedin"]}'
```

Response: `202 Accepted` with a post ID and status `queued`.

### 2. Watch the worker process it

```bash
# Docker Compose path:
docker compose logs worker --follow

# Kubernetes path:
kubectl logs -n publishhub -l app=publishhub-worker --follow
```

The worker claims the job, simulates publishing to each platform, and writes a terminal status.

### 3. Check the result

```bash
curl http://localhost:8080/api/v1/posts
```

### 4. Exercise autoscaling (Kubernetes path)

Submit a burst to trigger KEDA scale-up:

```bash
for i in $(seq 1 50); do
  curl -s -X POST http://localhost:8081/api/v1/publish \
    -H 'Content-Type: application/json' \
    -d "{\"content\": \"Burst post $i\", \"platforms\": [\"twitter\"]}" &
done
wait
kubectl get hpa -n publishhub --watch
```

Workers scale up to handle load, then cool down to zero when the queue empties.

### 5. Exercise canary delivery (Kubernetes path)

```bash
make rollout-exercise
```

This triggers a canary rollout with weight steps and demonstrates promote/abort behavior.

### 6. Exercise retry and dead-letter

Start the stack with a 100% failure rate to see retries and dead-lettering:

```bash
SIMULATE_FAILURE_RATE=1 make dev-up
```

Submit a post and watch the worker retry up to `MAX_ATTEMPTS` before dead-lettering the job.

## Security Notice

**The API has no authentication.** This is by design — PublishHub is an internal-facing gateway, not a public service. The API is:

- Bound to `127.0.0.1` in docker-compose (not reachable from LAN)
- Exposed only as `ClusterIP` in Kubernetes (no Ingress or LoadBalancer by default)
- Accessible locally only through `kubectl port-forward`

CORS is restricted to a configurable allow-list (`CORS_ORIGINS` environment variable). The wildcard `*` is permitted only when `NODE_ENV=development`. In any other environment, requests from unlisted origins are rejected.

If you expose this API on a network, understand that it accepts unauthenticated requests from any client that can reach it.

## Repository Layout

```
publishhub/
├── apps/
│   ├── api/            Node.js API gateway (Express + TypeScript)
│   ├── worker/         Python background worker
│   └── web/            React frontend (Vite + TypeScript)
├── helm/publishhub/    Helm chart (one chart, all environments)
├── argocd/             App of Apps bootstrap, Applications, Projects, Rollouts
├── terraform/          AWS infrastructure (VPC, EKS, SQS, ECR, IAM)
├── cli/publishctl/     Developer CLI wrapping platform operations
├── scripts/            Cluster setup, smoke tests, load tests, AI analyzer
├── observability/      Datadog agent values, monitors, dashboard
├── docs/               Architecture, message schema, runbooks
├── tests/integration/  End-to-end suite (docker compose driven)
├── .github/workflows/  CI (PR validation) and deploy (main → ECR → GitOps)
├── docker-compose.yaml Fast inner loop (no Kubernetes)
├── Makefile            Entry point — run `make help` for all targets
└── .gitignore          Covers secrets, state, deps, and env files
```

## Make Targets

Run `make` or `make help` to see every target with descriptions. Key targets:

| Target | Purpose |
|---|---|
| `make dev-up` | Start the docker-compose inner loop |
| `make dev-down` | Stop the docker-compose stack |
| `make cluster-up` | Create the kind cluster and local registry |
| `make apps-build` | Build and push images to the local registry |
| `make platform-install` | Install ArgoCD, KEDA, Argo Rollouts |
| `make argocd-sync` | Deploy via the App of Apps bootstrap |
| `make rollout-exercise` | Exercise canary promote and abort |
| `make lint` | Lint all services |
| `make test` | Run all unit and integration tests |
| `make clean` | Delete the cluster and registry |

## Further Documentation

- [Architecture (deep dive)](docs/architecture.md)
- [Message Schema](docs/message-schema.md)
- [Distributed Tracing](docs/distributed-tracing.md)
- [AWS Deployment Runbook](docs/runbooks/aws-deployment.md)

## License

See [LICENSE](LICENSE).
