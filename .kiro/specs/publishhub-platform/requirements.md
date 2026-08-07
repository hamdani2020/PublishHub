# Requirements Document

## Introduction

PublishHub is a production-like developer platform that mirrors a modern social-media publishing stack. It consists of three application services (a React web frontend, a Node.js API gateway, and a Python background worker) running on Kubernetes, managed end-to-end by a GitOps platform layer (ArgoCD, Argo Rollouts, KEDA), provisioned by Terraform on AWS, observed with Datadog, and operated with a purpose-built developer CLI plus an AI-assisted incident analyzer backed by Amazon Bedrock.

The system must be fully runnable on a local machine at zero cost (kind cluster + local registry + Redis queue) and deployable to AWS (EKS + SQS + ECR) without changing application code. The source of truth for the intended scope is `EXECUTION_GUIDE_PublishHub.md` at the repository root.

### Scope boundaries

- Publishing to real social platforms is **simulated**. No third-party social APIs are integrated.
- Authentication for end users is **out of scope**; the API is an internal-facing gateway. Where the API is exposed on a network, the lack of authentication must be explicit and documented.
- AWS deployment is opt-in and must be destroyable with a single command.

### Requirements summary

| # | Requirement | Phase in guide |
|---|---|---|
| 1 | Local Kubernetes environment | 1 |
| 2 | API gateway service | 2 |
| 3 | Background worker service | 2 |
| 4 | Web frontend | 2 |
| 5 | Queue abstraction (Redis / SQS) | 2, 5, 9 |
| 6 | Container images and local registry | 2 |
| 7 | Helm chart | 4 |
| 8 | GitOps delivery with ArgoCD | 4 |
| 9 | Event-driven autoscaling with KEDA | 3, 5 |
| 10 | Progressive delivery with Argo Rollouts | 3, 6 |
| 11 | Developer CLI (`publishctl`) | 7 |
| 12 | AI incident analyzer (Amazon Bedrock) | 8 |
| 13 | AWS infrastructure with Terraform | 9 |
| 14 | Observability with Datadog | 10 |
| 15 | CI/CD with GitHub Actions | 11 |
| 16 | Documentation and runbooks | 1, 15 |

---

## Requirements

### Requirement 1: Local Kubernetes Environment

**User Story:** As a developer, I want to create a complete local Kubernetes environment with a single command, so that I can run the whole platform without any cloud account or spend.

#### Acceptance Criteria

1. WHEN a developer runs `make cluster-up` THEN the system SHALL create a kind cluster named `publishhub-cluster` and a local Docker registry reachable at `localhost:5001`.
2. WHEN the cluster is created THEN the system SHALL connect the registry container to the kind Docker network AND configure the cluster's containerd to resolve `localhost:5001` so that in-cluster image pulls succeed.
3. WHEN `make cluster-up` is run a second time against an existing cluster THEN the system SHALL detect the existing cluster and registry and complete successfully without error or duplication.
4. WHEN cluster creation completes THEN the system SHALL print a readiness message identifying the cluster name and the registry address.
5. WHEN a developer runs `make clean` THEN the system SHALL delete the kind cluster and the registry container, leaving no orphaned Docker resources.
6. IF a required tool (docker, kind, kubectl, helm) is not installed THEN the system SHALL fail with a message naming the missing tool and its install command rather than an opaque error.
7. WHEN a developer runs `make help` THEN the system SHALL list every available target with a one-line description.

### Requirement 2: API Gateway Service

**User Story:** As a web client, I want an HTTP API that accepts publish requests and enqueues them for background processing, so that the user gets an immediate response while work happens asynchronously.

#### Acceptance Criteria

1. WHEN a client sends `POST /api/v1/publish` with a JSON body containing `content` and `platforms` THEN the system SHALL validate the payload, enqueue a job, and respond `202 Accepted` with a generated post `id` and status `queued`.
2. IF the request body is missing `content`, has empty `content`, exceeds the maximum content length, or contains an empty or unsupported `platforms` array THEN the system SHALL respond `400 Bad Request` with a machine-readable error code and a human-readable message, and SHALL NOT enqueue a job.
3. WHEN a client sends `GET /health` THEN the system SHALL respond `200` with process liveness information without checking downstream dependencies.
4. WHEN a client sends `GET /ready` THEN the system SHALL verify queue connectivity and respond `200` when reachable or `503` when not.
5. WHEN a client sends `GET /api/v1/posts` THEN the system SHALL return the list of recently submitted posts with their current status.
6. WHEN any request is handled THEN the system SHALL emit a structured JSON log line containing method, path, status code, duration in milliseconds, and a correlation id.
7. IF an unhandled error occurs THEN the system SHALL respond `500` with a generic message, SHALL NOT leak stack traces or internal identifiers to the client, and SHALL log the full error server-side.
8. WHEN the process receives `SIGTERM` THEN the system SHALL stop accepting new connections, finish in-flight requests, close queue connections, and exit within the configured grace period.
9. WHEN the API is exposed on a network THEN the system SHALL document that it has no authentication and SHALL restrict CORS to a configurable allow-list rather than defaulting to `*` in non-development environments.

### Requirement 3: Background Worker Service

**User Story:** As a platform operator, I want a worker that consumes queued publish jobs, so that publishing work is decoupled from request handling and can scale independently.

#### Acceptance Criteria

1. WHEN a job is available on the queue THEN the worker SHALL claim it, simulate publishing for each requested platform, and record a terminal status for the post.
2. WHEN the queue is empty THEN the worker SHALL wait using a blocking or long-poll receive rather than a busy loop, so that idle CPU usage stays near zero.
3. IF processing a job raises an error THEN the worker SHALL retry with exponential backoff up to a configured maximum attempt count.
4. WHEN a job exceeds the maximum attempt count THEN the worker SHALL move it to a dead-letter destination and SHALL NOT block the queue.
5. WHEN a job is processed THEN the worker SHALL emit a structured JSON log line containing the post id, platform results, attempt number, and duration.
6. WHEN the worker receives `SIGTERM` THEN the worker SHALL finish the in-flight job, avoid claiming new jobs, and exit within the configured grace period so that scale-down never loses a message.
7. WHEN the worker starts and the queue is unreachable THEN the worker SHALL retry connection with backoff and report unready rather than crash-looping instantly.

### Requirement 4: Web Frontend

**User Story:** As a user, I want a browser interface to compose a post, choose platforms, and submit it, so that I can exercise the platform end to end and see the result.

#### Acceptance Criteria

1. WHEN a user opens the web application THEN the system SHALL render a composer with a content text area, selectable target platforms, and a publish action.
2. WHEN a user submits a post THEN the system SHALL call the API and display a queued confirmation including the returned post id.
3. IF the API returns an error or is unreachable THEN the system SHALL display an actionable error message and SHALL keep the user's draft content intact.
4. WHILE a submission is in flight THE system SHALL disable the publish action and show a pending state to prevent duplicate submissions.
5. WHEN the recent posts list is displayed THEN the system SHALL show each post's id, truncated content, target platforms, and current status.
6. WHEN the interface is rendered THEN the system SHALL meet baseline accessibility requirements: every control has an accessible label, the form is operable by keyboard alone, status changes are announced to assistive technology via a live region, and text meets WCAG AA contrast ratios.
7. WHEN the frontend is built for a container THEN the system SHALL serve static assets from a web server that resolves the API base URL at runtime rather than requiring a rebuild per environment.

### Requirement 5: Queue Abstraction

**User Story:** As a developer, I want the same application code to run against Redis locally and Amazon SQS in AWS, so that local development is free and production uses managed infrastructure.

#### Acceptance Criteria

1. WHEN the API or worker starts THEN the system SHALL select a queue backend from configuration, supporting at minimum `redis` and `sqs`.
2. WHEN the backend is `redis` THEN the system SHALL use a Redis list as the job queue and a separate list as the dead-letter destination.
3. WHEN the backend is `sqs` THEN the system SHALL use an SQS queue with a configured redrive policy to an SQS dead-letter queue.
4. WHEN switching backends THEN the system SHALL require only environment variable changes, with no change to application business logic.
5. IF the configured backend is unknown or its required settings are missing THEN the system SHALL fail fast at startup with a message naming the offending configuration key.
6. WHEN a job is enqueued THEN the system SHALL serialize it using a single documented message schema that includes a schema version, so that both backends and both languages agree on the format.

### Requirement 6: Container Images and Local Registry

**User Story:** As a developer, I want reproducible, small container images for each service, so that builds are fast locally and safe in production.

#### Acceptance Criteria

1. WHEN a developer runs `make apps-build` THEN the system SHALL build images for api, worker, and web and push them to `localhost:5001`.
2. WHEN images are built THEN the system SHALL use multi-stage builds so that build toolchains and development dependencies are excluded from the final image.
3. WHEN a container starts THEN the system SHALL run the process as a non-root user with a read-only-friendly filesystem layout.
4. WHEN images are built THEN the system SHALL tag each image with both `latest` and an immutable tag derived from the Git commit SHA.
5. WHEN a developer queries `http://localhost:5001/v2/_catalog` after a build THEN the registry SHALL list `publishhub-api`, `publishhub-worker`, and `publishhub-web`.
6. WHEN a Dockerfile declares dependencies THEN the system SHALL pin base image tags and dependency versions rather than using floating ranges.

### Requirement 7: Helm Chart

**User Story:** As a platform engineer, I want one Helm chart that deploys the whole application stack, so that every environment is described by the same templates with different values.

#### Acceptance Criteria

1. WHEN the chart is rendered THEN the system SHALL produce Deployments and Services for api, worker, and web, a Redis workload for local use, and a KEDA ScaledObject for the worker.
2. WHEN the chart is rendered THEN every workload SHALL declare resource requests and limits, liveness and readiness probes, and a security context that disables privilege escalation and drops unnecessary capabilities.
3. WHEN environment-specific values are supplied THEN the system SHALL allow image repository and tag, replica counts, autoscaling thresholds, queue backend, and Datadog settings to be overridden per environment without template edits.
4. WHEN `helm lint` and `helm template` are run against the chart with the default and each environment values file THEN the system SHALL complete without errors.
5. IF a required value has no safe default THEN the chart SHALL fail rendering with an explicit message naming the missing value.
6. WHEN secrets are needed THEN the chart SHALL reference existing Kubernetes Secrets by name and SHALL NOT embed secret values in `values.yaml`.

### Requirement 8: GitOps Delivery with ArgoCD

**User Story:** As a platform engineer, I want ArgoCD to be the only component that applies changes to the cluster, so that Git is the single source of truth and drift is corrected automatically.

#### Acceptance Criteria

1. WHEN `make platform-install` is run THEN the system SHALL install ArgoCD, KEDA, and Argo Rollouts into their own namespaces and wait until each is ready before returning.
2. WHEN `make argocd-sync` is run THEN the system SHALL apply a bootstrap Application that manages all other Applications following the App of Apps pattern.
3. WHEN the bootstrap Application syncs THEN the system SHALL create the `publishhub` Application which deploys the Helm chart to the `publishhub` namespace with automated sync, self-healing, and pruning enabled.
4. WHEN an ArgoCD Project is defined THEN the system SHALL restrict which repositories, destination namespaces, and resource kinds the project may deploy.
5. IF a resource in the cluster diverges from Git THEN ArgoCD SHALL report the Application as `OutOfSync` and SHALL restore the Git-declared state.
6. WHEN a developer runs `make argocd-password` THEN the system SHALL retrieve the initial admin password from its Secret and print it without writing it to a tracked file.
7. WHEN repository URLs are required THEN the manifests SHALL read them from a single configurable location, so that a fork does not require editing every file.

### Requirement 9: Event-Driven Autoscaling with KEDA

**User Story:** As a platform operator, I want workers to scale on queue depth including scale-to-zero, so that we pay nothing when idle and absorb bursts automatically.

#### Acceptance Criteria

1. WHEN the worker ScaledObject is applied THEN KEDA SHALL create a corresponding HPA and report the ScaledObject as ready.
2. WHILE the queue is empty for the configured cooldown period THE system SHALL scale worker replicas to zero.
3. WHEN pending queue depth exceeds the configured target per replica THEN the system SHALL scale worker replicas up toward the configured maximum.
4. WHEN the queue is drained THEN the system SHALL scale worker replicas back down to the configured minimum after the cooldown period.
5. WHEN the queue backend is Redis THEN the ScaledObject SHALL use the Redis list scaler; WHEN the backend is SQS THEN it SHALL use the AWS SQS queue scaler authenticated through IRSA rather than static credentials.
6. WHEN scaling occurs THEN the system SHALL NOT drop or duplicate in-flight jobs, relying on the worker's graceful shutdown behavior.
7. WHEN a developer submits a burst of publish requests THEN the observed scaling behavior SHALL be verifiable through `kubectl get hpa`, ScaledObject status, and KEDA operator logs.

### Requirement 10: Progressive Delivery with Argo Rollouts

**User Story:** As a release engineer, I want API deployments to roll out as canaries with automatic rollback, so that a bad version affects a small fraction of traffic for a short time.

#### Acceptance Criteria

1. WHEN the API Rollout is applied THEN the system SHALL manage API replicas through a canary strategy with defined weight steps and pauses.
2. WHEN a new image is set on the Rollout THEN the system SHALL shift a small initial traffic weight to the canary and pause for analysis.
3. WHEN a developer promotes the Rollout THEN the system SHALL advance to the next weight step; WHEN a developer aborts THEN the system SHALL return all traffic to the stable version.
4. IF the configured failure condition is met during a canary step THEN the system SHALL automatically abort the rollout and restore the stable version without manual intervention.
5. WHEN a rollout is in progress THEN the system SHALL expose current step, desired weight, actual weight, and per-version image and replica counts through the Argo Rollouts CLI.
6. WHEN the Rollout replaces the API Deployment THEN the Helm chart SHALL render exactly one of the two for a given configuration, so that two controllers never fight over the same pods.

### Requirement 11: Developer CLI (`publishctl`)

**User Story:** As a developer, I want a single CLI that wraps common platform operations, so that I do not need to memorize kubectl, helm, and docker incantations.

#### Acceptance Criteria

1. WHEN a developer runs `publishctl --help` THEN the system SHALL list all commands with descriptions.
2. WHEN a developer runs `publishctl env start` THEN the system SHALL create the cluster, install the platform, build images, and deploy the application, reporting progress for each stage.
3. WHEN a developer runs `publishctl env stop` THEN the system SHALL tear down the local environment.
4. WHEN a developer runs `publishctl status` THEN the system SHALL display cluster, ArgoCD Application, pod, and ScaledObject state in a readable summary.
5. WHEN a developer runs `publishctl logs --service <name>` THEN the system SHALL stream logs for that service, supporting a follow option.
6. WHEN a developer runs `publishctl publish` with content THEN the system SHALL submit a post through the API and print the response.
7. WHEN a developer runs `publishctl incident --pod <name>` THEN the system SHALL invoke the AI incident analyzer for that pod.
8. IF a prerequisite is missing or a wrapped command fails THEN the CLI SHALL exit with a non-zero status and print the underlying error rather than a generic failure.
9. WHEN the CLI is installed with `pip install -e .` THEN the `publishctl` entry point SHALL be available on the developer's PATH.

### Requirement 12: AI Incident Analyzer (Amazon Bedrock)

**User Story:** As an on-call engineer, I want an assistant that gathers a failing pod's state and returns a structured root-cause analysis, so that triage at 2am starts from a hypothesis instead of a blank terminal.

#### Acceptance Criteria

1. WHEN the analyzer is invoked with a pod name and namespace THEN the system SHALL collect the pod description, recent logs, previous-container logs when the pod restarted, and related Kubernetes events.
2. WHEN diagnostic data is collected THEN the system SHALL send it to Amazon Bedrock using the Claude 3 Haiku model and request a report containing summary, ranked root-cause hypotheses, recommended fix, severity, and category.
3. WHEN the analysis returns THEN the system SHALL print a formatted report and SHALL support a JSON output mode for programmatic use.
4. IF Bedrock access is denied, the model is not enabled, or credentials are missing THEN the system SHALL print a clear diagnostic naming the likely cause and remediation and SHALL exit non-zero.
5. IF a `kubectl` command fails THEN the system SHALL include the failure in the report input rather than aborting the entire analysis.
6. WHEN diagnostic data is assembled THEN the system SHALL truncate each section to a documented character budget to bound token cost and SHALL redact values matching common secret patterns before transmission.
7. WHEN the analyzer runs THEN the system SHALL authenticate using the ambient AWS credential chain and SHALL NOT require or store an API key in the repository.

### Requirement 13: AWS Infrastructure with Terraform

**User Story:** As a platform engineer, I want the AWS footprint defined as code, so that it can be reviewed, reproduced, and destroyed reliably.

#### Acceptance Criteria

1. WHEN Terraform is applied THEN the system SHALL provision a VPC with public and private subnets across three availability zones, an EKS cluster with managed node groups, an SQS queue with a dead-letter queue, and ECR repositories for each service.
2. WHEN IAM is configured THEN the system SHALL create least-privilege roles and SHALL use IRSA for in-cluster workloads including the KEDA SQS scaler, with no long-lived access keys.
3. WHEN cost controls are configured THEN the system SHALL default to Spot capacity and ARM instance types where supported and SHALL apply an ECR lifecycle policy that expires untagged and surplus images.
4. WHEN `terraform validate` and `terraform plan` are run THEN the system SHALL complete without errors and the plan SHALL be reviewable before any apply.
5. WHEN Terraform completes THEN the system SHALL output the cluster name, region, ECR repository URLs, and SQS queue URL needed by the deployment pipeline.
6. WHEN `terraform destroy` is run THEN the system SHALL remove all created billable resources, and the documentation SHALL state this explicitly as the cost-control step.
7. WHEN state is configured THEN the system SHALL support a remote backend with locking, and SHALL NOT commit state files or `.tfvars` containing account-specific values.
8. WHEN any apply or destroy is proposed THEN the tooling SHALL require explicit human confirmation and SHALL NOT run automatically from a make target or CI job by default.

### Requirement 14: Observability with Datadog

**User Story:** As an operator, I want metrics, traces, and logs from every service, so that I can see system health and diagnose issues without shelling into pods.

#### Acceptance Criteria

1. WHEN the Datadog agent is installed THEN the system SHALL collect cluster metrics, container logs, and APM traces from the `publishhub` namespace.
2. WHEN a publish request flows through the system THEN the resulting trace SHALL span the API request and the worker's processing of that job, correlated by a propagated trace context.
3. WHEN application logs are emitted THEN they SHALL be structured JSON including service name, environment, and trace identifiers so that logs and traces correlate automatically.
4. WHEN business events occur THEN the system SHALL emit custom metrics for posts submitted, jobs processed, job failures, and queue depth.
5. WHEN monitors are defined THEN the system SHALL include alerts for API error rate, worker failure rate, queue depth growth, and pod crash looping, stored as code in the repository.
6. WHEN observability is disabled in values THEN the applications SHALL run normally with tracing and metric emission inert, so that local development requires no Datadog account.
7. WHEN Datadog credentials are supplied THEN they SHALL be provided through a Kubernetes Secret or CI secret and SHALL NOT appear in committed files or shell history examples.

### Requirement 15: CI/CD with GitHub Actions

**User Story:** As a developer, I want every push validated and every merge to main to produce deployed artifacts, so that delivery is automatic and traceable to a commit.

#### Acceptance Criteria

1. WHEN a pull request is opened THEN the pipeline SHALL run linting, type checking, unit tests, Helm lint and template, and Terraform validate, and SHALL build images without pushing them.
2. WHEN a commit is merged to main THEN the pipeline SHALL build and push images for api, worker, and web to ECR tagged with the commit SHA.
3. WHEN images are pushed THEN the pipeline SHALL update the image tags in the Helm values file and commit that change, so that ArgoCD deploys the new version through Git.
4. WHEN the pipeline authenticates to AWS THEN it SHALL use GitHub OIDC role assumption and SHALL NOT use stored access keys.
5. WHEN the pipeline commits back to the repository THEN it SHALL avoid triggering an infinite build loop.
6. WHEN images are built THEN the pipeline SHALL scan them for known vulnerabilities and SHALL fail the build on critical findings.
7. IF any required secret or variable is absent THEN the workflow SHALL fail with a message identifying the missing configuration.

### Requirement 16: Documentation and Runbooks

**User Story:** As a new contributor or interviewer, I want documentation that explains the architecture and how to run and operate the system, so that the project is understandable without a walkthrough.

#### Acceptance Criteria

1. WHEN a reader opens the repository README THEN the system SHALL present the architecture, the technology choices and their rationale, a quick-start sequence, and the demo flow.
2. WHEN a developer follows the local development guide from a clean machine THEN the documented steps SHALL produce a running stack with no undocumented manual actions.
3. WHEN an operator encounters a common failure THEN a runbook SHALL exist covering queue backlog, worker crash looping, failed ArgoCD sync, failed canary rollout, and Bedrock access errors.
4. WHEN documentation references commands THEN those commands SHALL match the actual make targets, CLI commands, and file paths in the repository.
5. WHEN the repository is published THEN it SHALL include a `.gitignore` and secret-handling guidance that prevent credentials, Terraform state, and local environment files from being committed.
