# Contributing to PublishHub

Thank you for your interest in contributing to PublishHub. This guide covers the
workflow, standards, and security practices for contributing to this repository.

---

## Getting Started

1. Fork the repository and clone your fork
2. Create a feature branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
3. Set up your local environment following `docs/local-dev.md`
4. Make your changes, following the guidelines below
5. Push your branch and open a Pull Request

---

## Development Workflow

### Prerequisites

Run `make help` to see available targets and `publishctl doctor` to verify all
required tools are installed.

### Running Locally

```bash
# Start the full local stack (kind cluster + platform + apps)
make cluster-up
make platform-install
make apps-build
make argocd-sync

# Or for fast inner-loop development without Kubernetes:
docker compose up
```

### Testing

Run the test suite before submitting:

```bash
make test    # runs all tests
make lint    # runs all linters
```

Service-specific tests:

```bash
# API (Node/TypeScript)
cd apps/api && npm test

# Worker (Python)
cd apps/worker && pytest

# Web (React)
cd apps/web && npm test

# Helm chart
helm lint helm/publishhub/
helm template publishhub helm/publishhub/ -f helm/publishhub/values.yaml
```

### Code Standards

- **TypeScript (API, Web):** Follow the ESLint configuration in each project.
  Run `npm run lint` and `npm run typecheck` before committing.
- **Python (Worker, CLI):** Follow PEP 8. Run `ruff check` and `ruff format
  --check` before committing.
- **Helm charts:** Must pass `helm lint` and render cleanly with `helm template`
  for both `values.yaml` and `values-production.yaml`.
- **Terraform:** Must pass `terraform fmt -check -recursive` and `terraform
  validate`.

### Commit Messages

Use clear, descriptive commit messages:

```
<scope>: <short description>

<optional body explaining why>
```

Scopes: `api`, `worker`, `web`, `helm`, `argocd`, `terraform`, `cli`, `docs`,
`ci`, `scripts`

---

## Pull Request Process

1. Ensure CI passes (lint, typecheck, tests, Trivy scan)
2. Update documentation if your change affects user-facing behavior
3. Add or update tests for new functionality
4. Keep PRs focused — one concern per PR
5. Reference the related requirement or issue in the PR description

---

## Secret Handling

> This section is critical for maintaining the security of the project and any
> deployed environments.

### Never commit secrets

The following must **never** be committed to this repository:

- AWS access keys, secret keys, or session tokens
- API keys or tokens of any kind
- Database connection strings with credentials
- Private keys or certificates (`.pem`, `.key`)
- Kubernetes kubeconfig files
- Environment files with real values

### Files that must stay gitignored

The `.gitignore` at the repository root prevents accidental commits of:

| Pattern | Purpose |
|---------|---------|
| `.env*` | Environment files with local credentials |
| `*.tfvars` | Terraform variable files with account-specific values |
| `*.tfvars.json` | Terraform variable files (JSON format) |
| `terraform.tfstate` | Terraform state containing resource details |
| `terraform.tfstate.*` | Terraform state backups |
| `.terraform/` | Terraform working directory with provider binaries |
| `backend.tf` | Terraform backend config with real bucket names |
| `kubeconfig*` | Kubernetes cluster access credentials |
| `*.pem` | Private keys and certificates |
| `*.key` | Private key files |

Do not modify the `.gitignore` to remove these patterns. If you need to add an
exception (like `.env.example`), use the `!` prefix pattern that already exists.

### Use placeholder values in examples

When adding documentation, configuration examples, or code samples:

- Use `123456789012` for AWS account IDs
- Use `your-org/publishhub` for repository references
- Use `https://sqs.us-east-1.amazonaws.com/123456789012/example` for AWS URLs
- Use `REPLACE_ME` or `<your-value-here>` for fields that need real values
- Never paste real keys, tokens, or connection strings into examples

### How secrets are managed in this project

PublishHub uses these mechanisms for secrets — none of which require storing
credentials in Git:

| Context | Mechanism |
|---------|-----------|
| In-cluster workloads (KEDA, worker) | IRSA (IAM Roles for Service Accounts) |
| GitHub Actions CI/CD | GitHub OIDC federation — no long-lived keys |
| Kubernetes Secrets | Referenced by name in Helm templates, values provided at deploy time |
| Local development | No secrets needed (Redis queue, no AWS) |
| Bedrock access | Ambient AWS credential chain (SSO, env vars, instance profile) |

### If you accidentally commit a secret

If a credential is committed, even in a branch that hasn't been merged:

1. **Rotate the credential immediately.** Assume it is compromised the moment
   it hits any remote.
2. **Remove it from Git history.** A force push or rebase does not remove it
   from reflog or forks. Use `git filter-repo` or `BFG Repo-Cleaner`:
   ```bash
   # Example with git filter-repo
   git filter-repo --path-glob '*.env' --invert-paths
   ```
3. **Notify the team** so anyone who pulled the branch can clean their local
   copy.
4. **Audit access logs** for the rotated credential to check for unauthorized
   use.

Simply deleting the file in a new commit is **not sufficient** — the secret
remains in the Git history and is recoverable.

---

## Project Structure

```
publishhub/
├── apps/{api,worker,web}/     Application services
├── helm/publishhub/           Helm chart
├── argocd/                    GitOps configuration
├── terraform/                 AWS infrastructure
├── cli/publishctl/            Developer CLI
├── scripts/                   Utility scripts
├── observability/             Datadog configuration
├── docs/                      Documentation and runbooks
├── .github/workflows/         CI/CD pipelines
├── docker-compose.yaml        Fast local development
└── Makefile                   Task runner
```

---

## Questions?

If you're unsure about something, open an issue to discuss before implementing.
For operational questions, check the [runbooks](docs/runbooks/) directory.
