# PublishHub — repository entry point.
#
# Run `make` or `make help` to list every target with a one-line description.
# Descriptions are read from the `## ...` comment on each target line, and
# `##@ ...` lines introduce a section heading (Requirement 1.7).
#
# Missing tools are reported by name together with an install command rather
# than an opaque shell error (Requirement 1.6). See `make check-tools`.
#
# DELIBERATELY ABSENT: there is no target that runs `terraform apply` or
# `terraform destroy`. Creating or destroying billable AWS resources must be an
# explicit, human-confirmed action and never a side effect of a make target or
# a CI job (Requirement 13.8). The read-only Terraform targets below (init,
# fmt, validate, plan) are safe; the apply/destroy procedure lives in the AWS
# deployment runbook instead.

SHELL := /bin/bash

.DEFAULT_GOAL := help

# --- Configuration -----------------------------------------------------------
# Every value is overridable from the environment or the command line, e.g.
#   make cluster-up CLUSTER_NAME=scratch

CLUSTER_NAME       ?= publishhub-cluster
REGISTRY_NAME      ?= publishhub-registry
REGISTRY_PORT      ?= 5001
REGISTRY           ?= localhost:$(REGISTRY_PORT)
APP_NAMESPACE      ?= publishhub
ARGOCD_NAMESPACE   ?= argocd
KEDA_NAMESPACE     ?= keda
ROLLOUTS_NAMESPACE ?= argo-rollouts
TERRAFORM_DIR      ?= terraform
GIT_SHA            := $(shell git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)

# Tools required for the local Kubernetes workflow (Requirement 1.6).
REQUIRED_TOOLS := docker kind kubectl helm

# Install command reported when a tool is missing. Keep one entry per tool that
# any target depends on; `require-<tool>` looks up INSTALL_HINT_<tool>.
INSTALL_HINT_docker := brew install --cask docker (or https://docs.docker.com/get-docker/)
INSTALL_HINT_kind := brew install kind (or https://kind.sigs.k8s.io/docs/user/quick-start/)
INSTALL_HINT_kubectl := brew install kubectl (or https://kubernetes.io/docs/tasks/tools/)
INSTALL_HINT_helm := brew install helm (or https://helm.sh/docs/intro/install/)
# INSTALL_HINT_terraform := brew install terraform (or https://developer.hashicorp.com/terraform/install)
INSTALL_HINT_python3 := brew install python@3.11 (or https://www.python.org/downloads/)
INSTALL_HINT_node := brew install node@20 (or https://nodejs.org/en/download)

# --- Internal helpers --------------------------------------------------------

# Honest placeholder for targets that later spec tasks implement, so that
# `make <target>` never looks like it did real work.
# Usage: $(call not-implemented,<target>,<task reference>)
define not-implemented
@printf '\n  %s is not implemented yet — nothing was changed.\n' '$(1)'
@printf '  Implemented by publishhub-platform spec task(s): %s\n\n' '$(2)'
endef

# --- Targets -----------------------------------------------------------------

##@ Help

.PHONY: help
help: ## List every target with a one-line description
	@awk 'BEGIN { \
	    FS = ":.*##"; \
	    printf "\nPublishHub — make targets\n\nUsage:\n  make <target>\n"; \
	  } \
	  /^##@/ { printf "\n%s\n", substr($$0, 5); next } \
	  /^[a-zA-Z0-9][a-zA-Z0-9_.-]*:.*##/ { printf "  %-20s %s\n", $$1, $$2 } \
	  END { printf "\n" }' $(MAKEFILE_LIST)

##@ Prerequisites

.PHONY: check-tools
check-tools: $(addprefix require-,$(REQUIRED_TOOLS)) ## Verify required tools are installed
	@printf 'All required tools found: %s\n' '$(REQUIRED_TOOLS)'

# Fails naming the tool and how to install it (Requirement 1.6).
# Used as a prerequisite, e.g. `cluster-up: require-docker require-kind`.
require-%:
	@tool='$*'; hint='$(INSTALL_HINT_$*)'; \
	if ! command -v "$$tool" >/dev/null 2>&1; then \
	  if [ -z "$$hint" ]; then \
	    hint='see docs/local-dev.md for installation instructions'; \
	  fi; \
	  printf 'ERROR: required tool not found: %s\n' "$$tool" >&2; \
	  printf '       install it with: %s\n' "$$hint" >&2; \
	  exit 1; \
	fi

.PHONY: config
config: ## Print the resolved configuration variables
	@printf 'CLUSTER_NAME      = %s\n' '$(CLUSTER_NAME)'
	@printf 'REGISTRY          = %s\n' '$(REGISTRY)'
	@printf 'REGISTRY_NAME     = %s\n' '$(REGISTRY_NAME)'
	@printf 'APP_NAMESPACE     = %s\n' '$(APP_NAMESPACE)'
	@printf 'ARGOCD_NAMESPACE  = %s\n' '$(ARGOCD_NAMESPACE)'
	@printf 'GIT_SHA           = %s\n' '$(GIT_SHA)'

##@ Inner loop (docker compose, no Kubernetes)

.PHONY: dev-up
dev-up: require-docker ## Start Redis, API, worker, and web with docker compose
	$(call not-implemented,dev-up,6.1)

.PHONY: dev-down
dev-down: require-docker ## Stop the docker compose stack
	$(call not-implemented,dev-down,6.1)

##@ Local Kubernetes cluster

.PHONY: cluster-up
cluster-up: require-docker require-kind require-kubectl ## Create the kind cluster and local registry
	$(call not-implemented,cluster-up,8.1 and 8.2)

.PHONY: clean
clean: require-docker require-kind ## Delete the kind cluster and the registry container
	$(call not-implemented,clean,8.2)

##@ Container images

.PHONY: apps-build
apps-build: require-docker ## Build api, worker, and web images and push to the local registry
	$(call not-implemented,apps-build,7.3)

##@ Platform layer and GitOps

.PHONY: platform-install
platform-install: require-kubectl require-helm ## Install ArgoCD, KEDA, and Argo Rollouts
	$(call not-implemented,platform-install,10.1)

.PHONY: argocd-sync
argocd-sync: require-kubectl ## Apply the ArgoCD bootstrap Application (App of Apps)
	$(call not-implemented,argocd-sync,10.3)

.PHONY: argocd-password
argocd-password: require-kubectl ## Print the initial ArgoCD admin password
	$(call not-implemented,argocd-password,10.3)

.PHONY: argocd-port-forward
argocd-port-forward: require-kubectl ## Forward the ArgoCD UI to localhost:8080
	$(call not-implemented,argocd-port-forward,10.3)

.PHONY: web-port-forward
web-port-forward: require-kubectl ## Forward the web frontend to localhost:3000
	$(call not-implemented,web-port-forward,10.3)

.PHONY: api-port-forward
api-port-forward: require-kubectl ## Forward the API to localhost:8081
	$(call not-implemented,api-port-forward,10.3)

##@ Quality gates

.PHONY: lint
lint: check-test-layout lint-api lint-worker ## Lint every service and the Helm chart
	@printf '\n  Web and Helm chart linting arrive with spec tasks 5.1 and 9.5.\n\n'

.PHONY: lint-api
lint-api: require-node ## Lint and typecheck the API (TypeScript)
	@cd apps/api && npm install --no-audit --no-fund --silent && npm run lint && npm run typecheck

# Same virtualenv as test-worker, so the two targets share one install.
.PHONY: lint-worker
lint-worker: require-python3 ## Lint the worker (Python) with ruff
	@cd apps/worker && \
	  { [ -d .venv ] || python3 -m venv .venv; } && \
	  .venv/bin/pip install --quiet --disable-pip-version-check -r requirements-dev.txt && \
	  .venv/bin/ruff check .

# Same check the `.kiro/hooks/test-layout-*` hooks run, so a misplaced test file
# fails the same way in the editor, in `make lint`, and in CI.
.PHONY: check-test-layout
check-test-layout: ## Fail if any test file sits beside production source
	@bash scripts/check-test-layout.sh

.PHONY: test
test: test-api test-worker ## Run unit and integration tests for every service
	@printf '\n  Web and integration suites arrive with spec tasks 5.x and 6.2.\n\n'

.PHONY: test-api
test-api: require-node ## Run the API (TypeScript) unit tests
	@cd apps/api && npm install --no-audit --no-fund --silent && npm test

# The virtualenv lives in apps/worker/.venv and is gitignored. Both suites read
# the shared contract fixture in contracts/, so a schema change fails here in
# whichever language drifted.
.PHONY: test-worker
test-worker: require-python3 ## Run the worker (Python) unit tests
	@cd apps/worker && \
	  { [ -d .venv ] || python3 -m venv .venv; } && \
	  .venv/bin/pip install --quiet --disable-pip-version-check -r requirements-dev.txt && \
	  .venv/bin/python -m pytest

##@ AWS infrastructure (read-only targets only)

.PHONY: tf-init
tf-init: require-terraform ## terraform init in the terraform directory
	$(call not-implemented,tf-init,15.3)

.PHONY: tf-fmt
tf-fmt: require-terraform ## terraform fmt -check -recursive
	$(call not-implemented,tf-fmt,15.3)

.PHONY: tf-validate
tf-validate: require-terraform ## terraform validate
	$(call not-implemented,tf-validate,15.3)

.PHONY: tf-plan
tf-plan: require-terraform ## terraform plan (review only, never applies)
	$(call not-implemented,tf-plan,15.3)

# There is intentionally no `tf-apply` or `tf-destroy` target: applying or
# destroying AWS infrastructure must be an explicit human action reviewed
# against a plan, never reachable from make or CI (Requirement 13.8).
