# Implementation Plan

Tasks are ordered so the stack is runnable as early as possible: the message contract comes first, then the three services with a docker-compose inner loop, then Kubernetes, then the platform layer, then AWS. Each task is independently verifiable.

Tasks marked **[manual]** contain steps that require a human decision or incur cost, and will stop for confirmation rather than execute automatically.

---

- [x] 1. Repository foundation
- [x] 1.1 Initialize the repository skeleton
  - Create the directory structure from the design's repository layout
  - Write `.gitignore` covering `node_modules`, `dist`, `__pycache__`, `.venv`, `.env*`, `*.tfvars`, `terraform.tfstate*`, `.terraform/`, `kubeconfig*`, `.DS_Store`
  - Initialize the Git repository on branch `main` with an initial commit
  - _Requirements: 16.5_

- [x] 1.2 Write the Makefile with a self-documenting help target
  - Implement `help` as the default target, parsing target comments into a description list
  - Add tool-check logic that fails with the missing tool's name and install command
  - Stub the targets the later tasks fill in: `cluster-up`, `platform-install`, `apps-build`, `argocd-sync`, `clean`, `test`, `lint`
  - Deliberately omit any target that runs `terraform apply` or `destroy`
  - _Requirements: 1.6, 1.7, 13.8_

- [x] 2. Message contract and queue abstraction
- [x] 2.1 Document the message schema and create the shared test fixture
  - Write `docs/message-schema.md` defining the versioned envelope and every field
  - Create a canonical JSON fixture under a shared path consumed by both test suites
  - _Requirements: 5.6_

- [x] 2.2 Implement the TypeScript queue client with Redis and SQS backends
  - Define the `QueueClient` interface and `PublishJob` type matching the schema
  - Implement the Redis backend using `LPUSH` and the `BRPOPLPUSH` reliable-queue pattern
  - Implement the SQS backend using `SendMessage` and long-polling `ReceiveMessage`
  - Implement a factory that selects the backend from `QUEUE_BACKEND` and fails fast naming any missing required config key
  - Write unit tests for both backends against a fake client, plus the schema fixture assertion
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

- [x] 2.3 Implement the Python queue client with Redis and SQS backends
  - Mirror the same interface, semantics, and factory behavior as the TypeScript client
  - Implement the stale-`processing`-entry reaper for the Redis backend
  - Write pytest coverage for both backends and assert against the same schema fixture
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 3.4_

- [x] 3. API service
- [x] 3.1 Scaffold the API project
  - Create `package.json` with pinned dependencies, `tsconfig.json`, and lint config
  - Add `build`, `dev`, `test`, `lint`, and `typecheck` scripts
  - _Requirements: 2.1, 6.6_

- [x] 3.2 Implement configuration loading and structured logging
  - Parse and validate every environment variable from the configuration reference, failing fast on invalid values
  - Configure `pino` for JSON output including service name, environment, and correlation id
  - Add request-logging middleware emitting method, path, status, duration, and correlation id
  - _Requirements: 2.6, 5.5, 14.3_

- [x] 3.3 Implement health and readiness endpoints
  - `/health` returns process liveness without touching dependencies
  - `/ready` checks Redis and queue reachability, returning 503 when unreachable
  - Write unit tests covering the healthy and dependency-down cases
  - _Requirements: 2.3, 2.4_

- [x] 3.4 Implement the publish endpoint with validation and the post store
  - Define the `zod` schema for `content` and `platforms` with the documented limits and allow-list
  - Implement the Redis post record store and recent-posts index
  - On success, persist the record, enqueue the job, and return `202` with id and status
  - Return the standard error envelope with `VALIDATION_FAILED` on bad input without enqueueing
  - Write unit tests for valid input, each validation failure, and queue-unavailable behavior
  - _Requirements: 2.1, 2.2_

- [x] 3.5 Implement the post query endpoints
  - `GET /api/v1/posts` returns recent posts newest-first with a capped limit
  - `GET /api/v1/posts/:id` returns one record or 404
  - Write unit tests including the empty-list and unknown-id cases
  - _Requirements: 2.5_

- [x] 3.6 Implement error handling, security middleware, and graceful shutdown
  - Add a central error handler returning the generic 500 envelope while logging the full error
  - Configure `helmet`, a body size limit, and CORS from `CORS_ORIGINS`, permitting `*` only in development
  - Implement `SIGTERM` handling: stop accepting connections, drain in-flight requests, close Redis and queue clients, exit within the grace period
  - Write unit tests asserting no stack trace leaks and that shutdown completes in order
  - _Requirements: 2.7, 2.8, 2.9_

- [x] 3.7 Add metrics instrumentation behind the observability switch
  - Expose `/metrics` with the counters from the design
  - Initialize `dd-trace` only when `OBSERVABILITY_ENABLED` is true; make the metrics client a no-op otherwise
  - Inject Datadog trace context into the message envelope when tracing is active
  - Write a unit test asserting the app runs identically with observability disabled
  - _Requirements: 14.4, 14.6, 14.2_

- [x] 4. Worker service
- [x] 4.1 Scaffold the worker project
  - Create `requirements.txt` with pinned versions and a pytest configuration
  - Implement configuration loading and validation for the worker's environment variables
  - Configure JSON structured logging with service, environment, and trace fields
  - _Requirements: 3.5, 5.5, 6.6, 14.3_

- [x] 4.2 Implement the job processing loop
  - Run the stale-entry reaper at startup for the Redis backend
  - Use blocking receive with `POLL_WAIT_SECONDS` so idle CPU stays near zero
  - Simulate per-platform publishing using `SIMULATE_LATENCY_MS` and `SIMULATE_FAILURE_RATE`
  - Write the terminal post status to Redis and ack the message on success
  - Write pytest coverage for the success path and for idle behavior
  - _Requirements: 3.1, 3.2_

- [x] 4.3 Implement retry, dead-lettering, and startup resilience
  - Retry failed jobs with exponential backoff up to `MAX_ATTEMPTS`
  - Dead-letter on attempt exhaustion, on unparseable payloads, and on unknown `schema_version`
  - Retry queue connection with backoff at startup instead of crash-looping
  - Write pytest coverage for exhaustion, poison messages, and unknown schema versions
  - _Requirements: 3.3, 3.4, 3.7_

- [x] 4.4 Implement graceful shutdown
  - Handle `SIGTERM` by setting a stop flag, finishing and acking the in-flight job, closing connections, and exiting
  - Write a test asserting a job received before `SIGTERM` is completed and acked, never lost
  - _Requirements: 3.6, 9.6_

- [x] 4.5 Add worker observability
  - Emit the custom metrics from the design for processed, failed, duration, and queue depth
  - Extract `trace_context` from the envelope and start a child span when tracing is enabled
  - Log every outcome with post id, platform results, attempt, and duration
  - _Requirements: 3.5, 14.2, 14.4, 14.6_

- [x] 5. Web frontend
- [x] 5.1 Scaffold the React application
  - Create the Vite + React + TypeScript project with pinned dependencies and `index.html`
  - Implement runtime config reading from `window.__PUBLISHHUB_CONFIG__` with a development fallback
  - _Requirements: 4.7, 6.6_

- [x] 5.2 Build the accessible composer form
  - Implement the content textarea and platform checkbox fieldset with real labels and a legend
  - Wire validation messages to inputs via `aria-describedby`
  - Ensure full keyboard operability and WCAG AA contrast in the stylesheet
  - Write Testing Library tests asserting labels, keyboard submission, and error association
  - _Requirements: 4.1, 4.6_

- [x] 5.3 Implement submission, pending, and result states
  - Call the API on submit and show a queued confirmation containing the returned post id
  - Disable the button and set `aria-busy` while in flight to prevent duplicate submissions
  - Announce the result through an `aria-live="polite"` region and move focus to it
  - Display an actionable message on API error or unreachability while preserving the draft
  - Write tests for the pending state, success announcement, and error-preserves-draft behavior
  - _Requirements: 4.2, 4.3, 4.4, 4.6_

- [x] 5.4 Implement the recent posts list
  - Fetch and render post id, truncated content, platforms, and status as an accessible list
  - Write a test for the populated and empty states
  - _Requirements: 4.5, 4.6_

- [x] 6. Local integration loop
- [x] 6.1 Create docker-compose for the fast inner loop
  - Define Redis, API, worker, and web services with the local environment configuration
  - Wire `make dev-up` and `make dev-down` to compose
  - _Requirements: 16.2_

- [x] 6.2 Write the end-to-end integration test
  - Submit a post through the API and assert it reaches a terminal status via the worker
  - Assert a forced-failure job lands in the dead-letter destination
  - Wire the test into `make test`
  - _Requirements: 2.1, 3.1, 3.4, 5.4_

- [x] 7. Container images
- [x] 7.1 Write the API and worker Dockerfiles
  - Use multi-stage builds with pinned base images that exclude build toolchains from the final layer
  - Run as a non-root user with correct signal handling so `SIGTERM` reaches the process
  - _Requirements: 6.2, 6.3, 6.6, 2.8, 3.6_

- [x] 7.2 Write the web Dockerfile, nginx config, and entrypoint
  - Build static assets, then serve them from `nginx-unprivileged` on port 8080
  - Proxy `/api` to the API Service so the browser makes same-origin requests
  - Generate `config.js` from environment variables at container start
  - _Requirements: 4.7, 6.2, 6.3_

- [x] 7.3 Implement the apps-build target
  - Build all three images tagged with both `latest` and the Git commit SHA, then push to the local registry
  - Verify the registry catalog lists all three repositories after the build
  - _Requirements: 6.1, 6.4, 6.5_

- [x] 8. Local Kubernetes cluster
- [x] 8.1 Write the kind cluster and registry script
  - Create or reuse the registry container and the `publishhub-cluster` kind cluster idempotently
  - Configure containerd's registry mirror for `localhost:5001` and connect the registry to the kind network
  - Print the readiness message naming the cluster and registry
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 8.2 Wire the cluster lifecycle make targets
  - Implement `cluster-up` and `clean`, with `clean` removing both the cluster and the registry container
  - Verify a repeated `cluster-up` succeeds without duplication and that `clean` leaves no orphaned resources
  - _Requirements: 1.3, 1.5_

- [x] 9. Helm chart
- [x] 9.1 Create the chart skeleton, helpers, and values files
  - Write `Chart.yaml`, `_helpers.tpl` for names, labels, selectors, and image references
  - Write `values.yaml` for kind defaults and `values-production.yaml` for ECR, SQS, and Datadog
  - Use `required` for values that have no safe default
  - _Requirements: 7.3, 7.5_

- [x] 9.2 Template the API, worker, web, and Redis workloads
  - Render Deployments and Services for all three services plus the Redis workload
  - Give every workload resource requests and limits, probes on the real endpoints, a hardened security context, and an appropriate termination grace period
  - Reference Secrets by name only; never inline secret values
  - _Requirements: 7.1, 7.2, 7.6_

- [x] 9.3 Template the KEDA ScaledObject with backend-aware triggers
  - Render the `redis` trigger or the `aws-sqs-queue` trigger with an IRSA authentication reference based on `queue.backend`
  - Parameterize min and max replicas, polling interval, cooldown, and target queue length
  - _Requirements: 9.5, 7.3_

- [x] 9.4 Template the API Rollout as a mutually exclusive alternative to the Deployment
  - Render either `api-deployment.yaml` or `api-rollout.yaml` based on `api.rollout.enabled`, never both
  - Add the canary and stable Services, and point the HPA's `scaleTargetRef` at the Rollout when it is active
  - _Requirements: 10.6, 7.1_

- [x] 9.5 Add chart tests and lint verification
  - Write helm unittest cases asserting Deployment/Rollout exclusivity, probes and limits on every workload, and correct trigger selection per backend
  - Verify `helm lint` and `helm template` pass for the default and production values files
  - _Requirements: 7.4, 7.2, 10.6_

- [x] 10. Platform layer and GitOps
- [x] 10.1 Implement the platform-install target
  - Install ArgoCD, KEDA, and Argo Rollouts into their own namespaces with pinned chart or manifest versions
  - Wait for each component's readiness before returning
  - Verify all expected pods reach Running in `argocd`, `keda`, and `argo-rollouts`
  - _Requirements: 8.1_

- [x] 10.2 Write the ArgoCD Project and Application manifests
  - Define the AppProject restricting source repositories, destination namespaces, and cluster-scoped resource kinds
  - Write `bootstrap.yaml` following App of Apps and the `publishhub` Application with automated sync, self-heal, and prune
  - Thread the repository URL through a single configurable location
  - _Requirements: 8.2, 8.3, 8.4, 8.7_

- [x] 10.3 Implement the sync, access, and credential targets
  - Implement `argocd-sync` applying the bootstrap Application, plus the port-forward targets for ArgoCD, web, and API
  - Implement `argocd-password` reading the initial admin secret without writing it to a tracked file
  - Verify both Applications appear, all `publishhub` pods run, and the ScaledObject exists
  - _Requirements: 8.2, 8.3, 8.6_

- [x] 10.4 Write the post-deploy smoke test script
  - Assert every pod is ready, the ScaledObject is ready, `/health` returns 200, and one submitted post reaches a terminal status
  - Verify self-healing by mutating a live resource and confirming ArgoCD restores it
  - _Requirements: 8.5, 9.1_

- [x] 11. Autoscaling verification
- [x] 11.1 Write the load generation script
  - Submit a configurable burst of publish requests against the port-forwarded API
  - _Requirements: 9.7_

- [x] 11.2 Write the scaling verification script
  - Assert workers sit at zero when idle, scale above one replica under the burst, and return to minimum after the queue drains
  - Assert no job is lost or duplicated across a scale-down, and surface HPA, ScaledObject, and KEDA operator state on failure
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.6, 9.7_

- [ ] 12. Progressive delivery
- [~] 12.1 Configure the canary strategy and analysis
  - Implement the six-step weight and pause sequence from the design
  - Add the Datadog-backed `AnalysisTemplate` with a local job-based fallback probe, wired so a failing check auto-aborts
  - _Requirements: 10.1, 10.2, 10.4_

- [~] 12.2 Write the rollout exercise script
  - Build and push a `v2` API image, set it on the Rollout, and report step, weights, and per-version images
  - Cover both promote and abort paths, asserting abort returns all traffic to stable
  - _Requirements: 10.2, 10.3, 10.5_

- [ ] 13. Developer CLI
- [~] 13.1 Scaffold the CLI package
  - Create `setup.py` with the `publishctl` entry point and pinned `click` and `rich` dependencies
  - Implement the subprocess helper using argument lists, surfacing exit codes and stderr
  - _Requirements: 11.1, 11.8, 11.9_

- [~] 13.2 Implement the doctor and environment commands
  - `doctor` verifies each prerequisite and reports the missing tool with its install command
  - `env start` and `env stop` orchestrate the full lifecycle with per-stage progress
  - _Requirements: 11.2, 11.3, 11.8, 1.6_

- [~] 13.3 Implement the operational commands
  - `status` summarizes cluster, Application, pod, and ScaledObject state
  - `logs` streams a service's logs with tail and follow options
  - `publish` submits a post through the API and prints the response
  - `scale` and `rollout status|promote|abort` wrap their respective operations
  - _Requirements: 11.4, 11.5, 11.6_

- [~] 13.4 Verify installation and help output
  - Install with `pip install -e .`, confirm `publishctl` resolves on PATH, and confirm `--help` lists every command
  - _Requirements: 11.1, 11.9_

- [ ] 14. AI incident analyzer
- [~] 14.1 Implement diagnostic collection with redaction
  - Collect pod description, recent logs, previous-container logs, and related events, recording rather than aborting on individual `kubectl` failures
  - Implement the secret redaction pass over all collected text before any transmission
  - Truncate each section to its documented character budget
  - Write unit tests for redaction patterns, truncation bounds, and the kubectl-failure path
  - _Requirements: 12.1, 12.5, 12.6_

- [~] 14.2 Implement Bedrock analysis with mapped error handling
  - Invoke Claude 3 Haiku with the structured SRE prompt requesting summary, ranked hypotheses, fix, severity, and category
  - Authenticate through the ambient AWS credential chain with no repository-stored key
  - Map each Bedrock and credential error to its actionable diagnostic and exit non-zero
  - _Requirements: 12.2, 12.4, 12.7_

- [~] 14.3 Implement report output and CLI integration
  - Render the formatted text report and implement the JSON output mode
  - Add the `publishctl incident` command delegating to the analyzer
  - Verify against a deliberately crashed pod that the report is produced end to end
  - _Requirements: 12.3, 11.7_

- [ ] 15. AWS infrastructure as code
- [~] 15.1 Write the VPC and EKS modules
  - VPC with public and private subnets across three AZs and a single NAT gateway
  - EKS with managed node groups defaulting to Spot capacity and ARM instance types, with the OIDC provider enabled
  - _Requirements: 13.1, 13.3_

- [~] 15.2 Write the SQS, ECR, and IAM modules
  - SQS main queue plus DLQ with a redrive policy
  - One ECR repository per service with a lifecycle policy expiring untagged and surplus images
  - Least-privilege IRSA roles for the KEDA SQS scaler and the worker, plus the GitHub OIDC role, with no long-lived keys
  - _Requirements: 13.1, 13.2, 13.3_

- [~] 15.3 Compose the root configuration, outputs, and backend example
  - Wire the modules in `main.tf` with variables and version constraints
  - Output cluster name, region, ECR repository URLs, and the SQS queue URL
  - Provide `backend.tf.example` for S3 state with DynamoDB locking, and confirm state and tfvars are gitignored
  - Verify `terraform fmt -check` and `terraform validate` pass
  - _Requirements: 13.4, 13.5, 13.7_

- [~] 15.4 Document the apply and destroy procedure **[manual]**
  - Write the AWS deployment runbook covering plan review, apply, `aws eks update-kubeconfig`, and production values
  - State the daily cost estimate and make `terraform destroy` a prominent, explicit teardown step
  - Do not add any make target or CI job that applies or destroys without confirmation
  - _Requirements: 13.6, 13.8, 16.3_

- [ ] 16. Observability
- [~] 16.1 Write the Datadog agent configuration
  - Provide the agent values enabling cluster metrics, log collection, and APM for the `publishhub` namespace
  - Supply credentials through a Kubernetes Secret reference, never a committed file
  - _Requirements: 14.1, 14.7_

- [~] 16.2 Verify distributed tracing across the API and worker
  - Confirm a publish request produces a single trace spanning the API handler and the worker's processing of that job
  - Confirm logs carry trace identifiers so log-trace correlation works
  - _Requirements: 14.2, 14.3_

- [~] 16.3 Define monitors and a dashboard as code
  - Write `monitors.yaml` for API error rate, worker failure rate, queue depth growth, pod crash looping, and workers stuck at zero with a non-empty queue
  - Write the dashboard definition covering request rate, latency, queue depth, and worker replica count
  - _Requirements: 14.5_

- [ ] 17. CI/CD
- [~] 17.1 Write the CI workflow
  - Run lint, typecheck, and unit tests per service; `helm lint` and `template` per values file; `terraform fmt -check` and `validate`
  - Build images without pushing on pull requests and scan them with Trivy, failing on critical findings
  - _Requirements: 15.1, 15.6_

- [~] 17.2 Write the deploy workflow
  - Add a preflight step asserting every required secret and variable is present, failing with the missing name
  - Assume the AWS role via OIDC, then build and push all three images to ECR tagged with the commit SHA
  - Update the Helm values image tags with `yq` and commit back, guarded against triggering an infinite build loop
  - _Requirements: 15.2, 15.3, 15.4, 15.5, 15.7_

- [ ] 18. Documentation
- [~] 18.1 Write the README and architecture document
  - Cover architecture, technology rationale, quick start, the demo flow, and the explicit statement that the API is unauthenticated
  - _Requirements: 16.1, 2.9_

- [~] 18.2 Write the local development guide and verify it from clean
  - Document every step from prerequisites to a running stack, then follow it end to end and correct any undocumented action
  - Verify all referenced commands, make targets, and paths match the repository
  - _Requirements: 16.2, 16.4_

- [~] 18.3 Write the operational runbooks
  - Cover queue backlog, worker crash looping, failed ArgoCD sync, failed canary rollout, and Bedrock access denial
  - Add secret-handling guidance to the contributing documentation
  - _Requirements: 16.3, 16.5_
