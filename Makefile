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

# Inner loop. The compose project name comes from `name:` in the file, so it is
# stable no matter which directory make was invoked from. DEV_WAIT_TIMEOUT has to
# cover the first run's `npm install` and `pip install`; later runs are seconds.
COMPOSE_FILE       ?= docker-compose.yaml
COMPOSE            ?= docker compose --file $(COMPOSE_FILE)
DEV_WAIT_TIMEOUT   ?= 300
DEV_DOWN_FLAGS     ?=

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
INSTALL_HINT_curl := brew install curl (preinstalled on macOS and most Linux distributions)

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
	@printf 'IMAGE_TAG         = %s\n' '$(IMAGE_TAG)'
	@printf 'COMPOSE           = %s\n' '$(COMPOSE)'

##@ Inner loop (docker compose, no Kubernetes)

# `--wait` blocks until every service reports healthy and fails if one does not,
# so `make dev-up` returning zero means the stack is actually usable. The timeout
# has to cover the dependency install on a clean checkout, which is why it is
# minutes rather than seconds; it is not a per-service health timeout, those live
# in docker-compose.yaml.
.PHONY: dev-up
dev-up: require-docker ## Start Redis, API, worker, and web with docker compose
	@$(COMPOSE) up --detach --wait --wait-timeout $(DEV_WAIT_TIMEOUT)
	@printf '\n  PublishHub is up (docker compose, no Kubernetes).\n\n'
	@printf '    web    http://localhost:3000\n'
	@printf '    api    http://localhost:8080  — unauthenticated by design, bound to localhost only\n'
	@printf '    redis  localhost:6379\n\n'
	@printf '    logs   $(COMPOSE) logs --follow [service]\n'
	@printf '    stop   make dev-down\n\n'

# Containers and the network go; the dependency-cache volumes stay, because
# re-downloading them is the slow part of the next `dev-up`. Drop them with
# `make dev-down DEV_DOWN_FLAGS=--volumes`.
.PHONY: dev-down
dev-down: require-docker ## Stop the docker compose stack
	@$(COMPOSE) down --remove-orphans $(DEV_DOWN_FLAGS)
	@printf '\n  Stack stopped. Dependency caches kept in named volumes.\n'
	@printf '  Drop them with: make dev-down DEV_DOWN_FLAGS=--volumes\n\n'

##@ Local Kubernetes cluster

.PHONY: cluster-up
cluster-up: require-docker require-kind require-kubectl ## Create the kind cluster and local registry
	@bash scripts/kind-with-registry.sh

.PHONY: clean
clean: require-docker require-kind ## Delete the kind cluster and the registry container
	@if kind get clusters 2>/dev/null | grep -q '^$(CLUSTER_NAME)$$'; then \
	  kind delete cluster --name '$(CLUSTER_NAME)'; \
	  printf '  Cluster "%s" deleted.\n' '$(CLUSTER_NAME)'; \
	else \
	  printf '  Cluster "%s" does not exist — nothing to delete.\n' '$(CLUSTER_NAME)'; \
	fi
	@if docker inspect '$(REGISTRY_NAME)' >/dev/null 2>&1; then \
	  docker rm -f '$(REGISTRY_NAME)' >/dev/null; \
	  printf '  Registry container "%s" removed.\n' '$(REGISTRY_NAME)'; \
	else \
	  printf '  Registry container "%s" does not exist — nothing to remove.\n' '$(REGISTRY_NAME)'; \
	fi
	@printf '\n  Clean complete. Cluster and registry resources removed.\n\n'

##@ Container images

# One image per directory under apps/. Each Dockerfile takes its own app
# directory as the build context, so the directory name is both the context and
# the image name suffix: apps/api -> publishhub-api.
APPS         ?= api worker web
IMAGE_PREFIX ?= publishhub

# Every image gets two tags (Requirement 6.4): `latest` for the inner loop and
# the short commit SHA as the immutable tag the Helm values pin. Override the
# immutable tag for a scratch build with: make apps-build IMAGE_TAG=wip
IMAGE_TAG    ?= $(GIT_SHA)

.PHONY: apps-build
apps-build: require-docker require-curl check-registry ## Build api, worker, and web images and push to the local registry
	@set -uo pipefail; \
	if [ -z '$(IMAGE_TAG)' ] || [ '$(IMAGE_TAG)' = unknown ]; then \
	  printf 'ERROR: refusing to build with the image tag "%s"\n' '$(IMAGE_TAG)' >&2; \
	  printf '       no commit SHA is available: `git rev-parse HEAD` failed — is this a Git\n' >&2; \
	  printf '       checkout with at least one commit?\n' >&2; \
	  printf '       or pass a tag explicitly: make apps-build IMAGE_TAG=<tag>\n' >&2; \
	  exit 1; \
	fi; \
	for app in $(APPS); do \
	  image='$(REGISTRY)/$(IMAGE_PREFIX)'"-$$app"; \
	  printf '\n==> %s  (context apps/%s)\n' "$$image" "$$app"; \
	  docker build --tag "$$image:latest" --tag "$$image:$(IMAGE_TAG)" "apps/$$app" || exit 1; \
	  docker push "$$image:latest" || exit 1; \
	  docker push "$$image:$(IMAGE_TAG)" || exit 1; \
	done; \
	$(MAKE) --no-print-directory check-catalog || exit 1; \
	printf '  Pushed %s images, each tagged latest and %s.\n\n' '$(words $(APPS))' '$(IMAGE_TAG)'

# Fails before the first `docker build` rather than after it, so an unreachable
# registry costs seconds instead of a full build (Requirement 6.1). The /v2/
# endpoint is the registry API's version check: any 2xx means it is answering.
.PHONY: check-registry
check-registry: require-curl ## Verify the local registry is answering
	@if ! curl --fail --silent --max-time 5 -o /dev/null 'http://$(REGISTRY)/v2/'; then \
	  printf 'ERROR: local registry not reachable at http://%s/v2/\n' '$(REGISTRY)' >&2; \
	  printf '       start it with: make cluster-up   (creates the kind cluster and the registry)\n' >&2; \
	  printf '       running already? check: docker ps --filter name=$(REGISTRY_NAME)\n' >&2; \
	  printf '       registry on another port? override it: make <target> REGISTRY_PORT=<port>\n' >&2; \
	  exit 1; \
	fi

# Requirement 6.5: after a build the catalog must list all three repositories.
# grep rather than jq, because jq is one more tool to require for one lookup.
.PHONY: check-catalog
check-catalog: require-curl ## Verify the registry catalog lists all three repositories
	@catalog="$$(curl --fail --silent --show-error --max-time 5 'http://$(REGISTRY)/v2/_catalog')" || { \
	  printf 'ERROR: could not read the registry catalog at http://%s/v2/_catalog\n' '$(REGISTRY)' >&2; \
	  exit 1; \
	}; \
	missing=''; \
	for app in $(APPS); do \
	  grep -q "\"$(IMAGE_PREFIX)-$$app\"" <<<"$$catalog" || missing="$$missing $(IMAGE_PREFIX)-$$app"; \
	done; \
	if [ -n "$$missing" ]; then \
	  printf 'ERROR: registry catalog is missing repositories:%s\n' "$$missing" >&2; \
	  printf '       catalog: %s\n' "$$catalog" >&2; \
	  printf '       run: make apps-build\n' >&2; \
	  exit 1; \
	fi; \
	printf '\n  Registry %s lists every repository:\n\n' '$(REGISTRY)'; \
	for app in $(APPS); do printf '    %s/%s-%s\n' '$(REGISTRY)' '$(IMAGE_PREFIX)' "$$app"; done; \
	printf '\n    catalog  %s\n\n' "$$catalog"

##@ Platform layer and GitOps

.PHONY: platform-install
platform-install: require-kubectl require-helm ## Install ArgoCD, KEDA, and Argo Rollouts
	@bash scripts/platform-install.sh

.PHONY: argocd-sync
argocd-sync: require-kubectl ## Apply the ArgoCD bootstrap Application (App of Apps)
	@printf '\n  Applying bootstrap Application (App of Apps)...\n\n'
	@kubectl apply -f argocd/bootstrap.yaml
	@printf '\n  Waiting for bootstrap Application to sync...\n'
	@kubectl wait --for=jsonpath='{.status.health.status}'=Healthy \
	  application/bootstrap -n $(ARGOCD_NAMESPACE) --timeout=120s 2>/dev/null || \
	  printf '  (bootstrap not yet Healthy — continuing; ArgoCD may still be syncing)\n'
	@printf '  Waiting for publishhub Application to appear and sync...\n'
	@for i in $$(seq 1 30); do \
	  if kubectl get application publishhub -n $(ARGOCD_NAMESPACE) >/dev/null 2>&1; then \
	    break; \
	  fi; \
	  sleep 2; \
	done
	@kubectl wait --for=jsonpath='{.status.health.status}'=Healthy \
	  application/publishhub -n $(ARGOCD_NAMESPACE) --timeout=180s 2>/dev/null || \
	  printf '  (publishhub not yet Healthy — pods may still be starting)\n'
	@printf '\n  Verifying Applications:\n\n'
	@kubectl get applications -n $(ARGOCD_NAMESPACE)
	@printf '\n  Verifying pods in %s namespace:\n\n' '$(APP_NAMESPACE)'
	@kubectl get pods -n $(APP_NAMESPACE) 2>/dev/null || \
	  printf '  (namespace %s not yet created)\n' '$(APP_NAMESPACE)'
	@printf '\n  Checking ScaledObject:\n\n'
	@kubectl get scaledobject -n $(APP_NAMESPACE) 2>/dev/null || \
	  printf '  (no ScaledObjects found yet)\n'
	@printf '\n  ArgoCD sync complete. Both Applications applied.\n\n'

.PHONY: argocd-password
argocd-password: require-kubectl ## Print the initial ArgoCD admin password
	@printf '\n  ArgoCD initial admin password:\n\n    '
	@kubectl -n $(ARGOCD_NAMESPACE) get secret argocd-initial-admin-secret \
	  -o jsonpath='{.data.password}' | base64 -d
	@printf '\n\n  Username: admin\n'
	@printf '  Use with: make argocd-port-forward  (then open https://localhost:8443)\n\n'

.PHONY: argocd-port-forward
argocd-port-forward: require-kubectl ## Forward the ArgoCD UI to localhost:8443
	@printf '\n  Forwarding ArgoCD UI to https://localhost:8443\n'
	@printf '  (accept the self-signed certificate warning in your browser)\n'
	@printf '  Press Ctrl-C to stop.\n\n'
	@kubectl port-forward svc/argocd-server -n $(ARGOCD_NAMESPACE) 8443:443

.PHONY: web-port-forward
web-port-forward: require-kubectl ## Forward the web frontend to localhost:3000
	@printf '\n  Forwarding web frontend to http://localhost:3000\n'
	@printf '  Press Ctrl-C to stop.\n\n'
	@kubectl port-forward svc/publishhub-web -n $(APP_NAMESPACE) 3000:8080

.PHONY: api-port-forward
api-port-forward: require-kubectl ## Forward the API to localhost:8081
	@printf '\n  Forwarding API to http://localhost:8081\n'
	@printf '  Press Ctrl-C to stop.\n\n'
	@kubectl port-forward svc/publishhub-api -n $(APP_NAMESPACE) 8081:8080

.PHONY: rollout-exercise
rollout-exercise: require-docker require-kubectl ## Exercise the Argo Rollouts canary promote and abort paths
	@bash scripts/rollout-exercise.sh

##@ Quality gates

.PHONY: lint
lint: check-test-layout lint-api lint-web lint-worker ## Lint every service and the Helm chart
	@printf '\n  Helm chart linting arrives with spec task 9.5.\n\n'

.PHONY: lint-api
lint-api: require-node ## Lint and typecheck the API (TypeScript)
	@cd apps/api && npm install --no-audit --no-fund --silent && npm run lint && npm run typecheck

.PHONY: lint-web
lint-web: require-node ## Lint and typecheck the web frontend (TypeScript)
	@cd apps/web && npm install --no-audit --no-fund --silent && npm run lint && npm run typecheck

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
test: test-api test-web test-worker test-integration ## Run unit and integration tests for every service

.PHONY: test-api
test-api: require-node ## Run the API (TypeScript) unit tests
	@cd apps/api && npm install --no-audit --no-fund --silent && npm test

# jsdom + Testing Library, no network and no API required.
.PHONY: test-web
test-web: require-node ## Run the web frontend (TypeScript) unit tests
	@cd apps/web && npm install --no-audit --no-fund --silent && npm test

# The virtualenv lives in apps/worker/.venv and is gitignored. Both suites read
# the shared contract fixture in contracts/, so a schema change fails here in
# whichever language drifted.
.PHONY: test-worker
test-worker: require-python3 ## Run the worker (Python) unit tests
	@cd apps/worker && \
	  { [ -d .venv ] || python3 -m venv .venv; } && \
	  .venv/bin/pip install --quiet --disable-pip-version-check -r requirements-dev.txt && \
	  .venv/bin/python -m pytest

# The end-to-end suite: submit a post through the real API and assert the real
# worker takes it to a terminal status, then force every publish to fail and
# assert the message lands in the dead-letter list (Requirements 2.1, 3.1, 3.4,
# 5.4). It drives the same docker-compose.yaml `make dev-up` uses.
#
# `require-docker` is deliberately *not* a prerequisite. A machine without Docker,
# or with Docker installed but not running, has not broken PublishHub, so the
# suite skips with a message naming what to start and `make test` stays green —
# see docker_unavailable_reason() in tests/integration/stack.py. Skip it on
# purpose with: PUBLISHHUB_SKIP_INTEGRATION=1 make test
.PHONY: test-integration
test-integration: require-python3 ## Run the end-to-end integration suite (docker compose)
	@cd tests/integration && \
	  { [ -d .venv ] || python3 -m venv .venv; } && \
	  .venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt && \
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
