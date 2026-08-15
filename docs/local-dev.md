# Local Development Guide

This guide takes you from a clean machine to a running PublishHub stack. Two paths are available:

1. **Fast path** (Docker Compose) — Redis, API, worker, and web running in seconds with live-reload. Best for application development.
2. **Full Kubernetes path** (kind cluster) — the complete platform including ArgoCD, KEDA, Argo Rollouts, and the Helm chart. Best for platform work and proving the deployment.

Both paths produce a working stack with no undocumented manual steps.

---

## Prerequisites

| Tool | Minimum version | Install (macOS) | Purpose |
|------|----------------|-----------------|---------|
| Docker | 24+ | `brew install --cask docker` | Container runtime |
| Node.js | 24.x | `brew install node@24` | API and web frontend |
| Python | 3.11+ | `brew install python@3.11` | Worker and CLI |
| Git | 2.x | Preinstalled on macOS | Source control |

The full Kubernetes path additionally requires:

| Tool | Install (macOS) | Purpose |
|------|-----------------|---------|
| kind | `brew install kind` | Local Kubernetes cluster |
| kubectl | `brew install kubectl` | Cluster interaction |
| helm | `brew install helm` | Chart rendering and deployment |
| curl | Preinstalled | Registry health checks |

Verify with:

```sh
make check-tools
```

This target reports each missing tool by name along with its install command.

---

## Clone and initial setup

```sh
git clone <repository-url>
cd publishhub
```

No other setup is needed before running either path. Dependencies install automatically on first start.

---

## Fast path: Docker Compose inner loop

This is the recommended path for day-to-day application work. It starts Redis, the API, the worker, and the web frontend with your working tree mounted into each container. Editing a file reloads the service that owns it.

### Start the stack

```sh
make dev-up
```

This runs `docker compose up --detach --wait` and blocks until every service is healthy. The first run installs dependencies inside the containers (npm packages for API and web, pip packages for worker), which takes 1-2 minutes. Subsequent runs start in seconds because dependency caches live in named Docker volumes.

When it completes:

| Service | URL | Notes |
|---------|-----|-------|
| Web | http://localhost:3000 | React dev server with hot reload |
| API | http://localhost:8080 | Express with tsx watch (auto-restart on save) |
| Redis | localhost:6379 | No persistence, data resets with the stack |

### Use the stack

1. Open http://localhost:3000 in your browser
2. Compose a post, select platforms, and submit
3. The post flows through the API, into the Redis queue, and the worker processes it

Watch logs:

```sh
docker compose logs --follow        # all services
docker compose logs --follow api    # one service
docker compose logs --follow worker
```

### Exercise retry and dead-letter paths

Start the stack with simulated failures:

```sh
SIMULATE_FAILURE_RATE=1 make dev-up
```

This makes the worker fail every publish attempt, exercising the retry logic and dead-letter path. Set it to a value between 0 and 1 for partial failure.

### Stop the stack

```sh
make dev-down
```

Containers and the network are removed. Dependency-cache volumes are kept so the next start is fast. Drop everything including caches with:

```sh
make dev-down DEV_DOWN_FLAGS=--volumes
```

---

## Full Kubernetes path

This path creates a kind cluster with a local registry, builds container images, installs the platform layer (ArgoCD, KEDA, Argo Rollouts), and deploys the application through GitOps.

### 1. Create the cluster and local registry

```sh
make cluster-up
```

This creates:
- A kind cluster named `publishhub-cluster`
- A local Docker registry at `localhost:5001`
- Network connectivity between the two so in-cluster image pulls work

Running it again when the cluster already exists is safe — it detects and skips existing resources.

### 2. Build and push images

```sh
make apps-build
```

This builds images for `api`, `worker`, and `web`, tags each with `latest` and the current Git SHA, and pushes them to the local registry. Verify:

```sh
curl -s http://localhost:5001/v2/_catalog
```

You should see `publishhub-api`, `publishhub-worker`, and `publishhub-web` listed.

### 3. Install the platform layer

```sh
make platform-install
```

Installs ArgoCD, KEDA, and Argo Rollouts into their own namespaces and waits for readiness.

### 4. Deploy the application with ArgoCD

```sh
make argocd-sync
```

This applies the bootstrap Application (App of Apps pattern), which creates the `publishhub` Application. ArgoCD then deploys the Helm chart to the `publishhub` namespace.

### 5. Access the services

ArgoCD UI:

```sh
make argocd-password    # prints the initial admin password
make argocd-port-forward  # forwards to https://localhost:8443
```

Application services:

```sh
make web-port-forward   # web frontend at http://localhost:3000
make api-port-forward   # API at http://localhost:8081
```

### 6. Verify the deployment

Check pods are running:

```sh
kubectl get pods -n publishhub
```

Check the ScaledObject:

```sh
kubectl get scaledobject -n publishhub
```

### 7. Exercise the canary rollout

```sh
make rollout-exercise
```

This promotes and aborts a canary deployment to demonstrate the Argo Rollouts workflow.

### Tear down

```sh
make clean
```

Deletes the kind cluster and the registry container.

---

## Running tests

### All tests

```sh
make test
```

Runs unit tests for all services plus the integration suite.

### Individual services

```sh
make test-api       # API unit tests (Vitest)
make test-web       # Web frontend tests (Vitest + Testing Library)
make test-worker    # Worker unit tests (pytest)
make test-integration  # End-to-end suite (docker compose, pytest)
```

The integration tests start the Docker Compose stack, submit posts through the API, and verify the worker processes them. They skip gracefully if Docker is unavailable.

### Running tests directly

API:

```sh
cd apps/api
npm install
npm test            # single run
npm run test:watch  # watch mode
```

Web:

```sh
cd apps/web
npm install
npm test            # single run
npm run test:watch  # watch mode
```

Worker:

```sh
cd apps/worker
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

---

## Linting

### All services

```sh
make lint
```

### Individual services

```sh
make lint-api       # ESLint + TypeScript type check
make lint-web       # ESLint + TypeScript type check
make lint-worker    # ruff
```

### Running linters directly

API:

```sh
cd apps/api
npm run lint        # ESLint
npm run typecheck   # tsc --noEmit
```

Web:

```sh
cd apps/web
npm run lint
npm run typecheck
```

Worker:

```sh
cd apps/worker
.venv/bin/ruff check .
```

---

## Working on individual services

Each service can run standalone for focused development.

### API (apps/api)

```sh
cd apps/api
npm install
npm run dev         # starts tsx watch on src/index.ts
```

The API needs Redis at `redis://localhost:6379` by default. Either run `make dev-up` first (which starts Redis), or start Redis manually:

```sh
docker run --rm -p 6379:6379 redis:7.4.10-alpine3.21
```

### Web frontend (apps/web)

```sh
cd apps/web
npm install
npm run dev         # Vite dev server at http://localhost:3000
```

The dev server proxies `/api` to `http://localhost:8080` by default (set by `VITE_DEV_API_TARGET`). Make sure the API is running.

### Worker (apps/worker)

```sh
cd apps/worker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=src REDIS_URL=redis://localhost:6379 .venv/bin/python -m publishhub_worker
```

`PYTHONPATH=src` is required so the package is importable without an editable install (pytest.ini sets this automatically for tests).

---

## Installing the developer CLI

The `publishctl` CLI wraps common platform operations.

```sh
cd cli/publishctl
python3 -m venv .venv
.venv/bin/pip install -e .
```

To put it on your PATH (activate the virtualenv):

```sh
source cli/publishctl/.venv/bin/activate
publishctl --help
```

Or reference it directly:

```sh
cli/publishctl/.venv/bin/publishctl --help
```

---

## Environment variables

Key variables you can override. All have sensible defaults for local development.

| Variable | Default | Effect |
|----------|---------|--------|
| `SIMULATE_FAILURE_RATE` | `0` | Worker failure probability (0.0–1.0) |
| `SIMULATE_LATENCY_MS` | `500` | Per-platform simulated publish time |
| `MAX_ATTEMPTS` | `3` | Retries before dead-letter |
| `POLL_WAIT_SECONDS` | `5` (compose) / `20` (chart) | Worker blocking receive window |
| `OBSERVABILITY_ENABLED` | `false` | Datadog tracing and metrics |

Pass them to Docker Compose through the environment:

```sh
SIMULATE_FAILURE_RATE=0.5 MAX_ATTEMPTS=5 make dev-up
```

---

## Common troubleshooting

### `make dev-up` times out

The first run downloads images and installs dependencies. Increase the timeout:

```sh
make dev-up DEV_WAIT_TIMEOUT=600
```

### Port already in use

The stack binds ports 3000, 6379, and 8080 on localhost. Stop conflicting processes or use Docker Compose directly with a different port mapping.

### `make apps-build` fails with "registry not reachable"

The local registry runs as a Docker container started by `make cluster-up`. Make sure it is running:

```sh
docker ps --filter name=publishhub-registry
```

If not, run `make cluster-up` first.

### Worker not processing jobs

Check the worker logs:

```sh
docker compose logs worker
```

Common causes:
- Redis not yet healthy (wait for the health check)
- `SIMULATE_FAILURE_RATE=1` set — all jobs go to the dead-letter list after `MAX_ATTEMPTS` retries

### ArgoCD sync pending

After `make argocd-sync`, applications may take a minute to reconcile. Check status:

```sh
kubectl get applications -n argocd
```

### Node version mismatch

The API requires Node 24+ (due to dd-trace 6.x and npm 11 lockfile format). The web frontend works with Node 20+. If you see npm errors about the lockfile version, upgrade Node.

### Python virtualenv issues

If `make test-worker` or `make lint-worker` fails on first run, the virtualenv may be stale. Remove and recreate:

```sh
rm -rf apps/worker/.venv
make test-worker
```

---

## Useful make targets at a glance

Run `make help` for the full list. Key targets:

| Target | Description |
|--------|-------------|
| `make check-tools` | Verify required tools are installed |
| `make dev-up` | Start Docker Compose inner loop |
| `make dev-down` | Stop Docker Compose stack |
| `make cluster-up` | Create kind cluster and local registry |
| `make clean` | Delete kind cluster and registry |
| `make apps-build` | Build and push images to local registry |
| `make platform-install` | Install ArgoCD, KEDA, Argo Rollouts |
| `make argocd-sync` | Deploy via ArgoCD App of Apps |
| `make argocd-password` | Print ArgoCD admin password |
| `make lint` | Lint all services |
| `make test` | Run all tests |
| `make config` | Print resolved configuration variables |
