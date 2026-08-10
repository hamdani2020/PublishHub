#!/usr/bin/env bash
# scripts/platform-install.sh
#
# Install the platform layer components (ArgoCD, KEDA, Argo Rollouts) into
# their own namespaces using Helm with pinned chart versions. Each component
# is installed sequentially, waiting for readiness before moving on.
#
# This is intended as a one-time bootstrap for the local kind cluster. After
# this step, ArgoCD owns the cluster and all further changes come through Git.
#
# Configuration (environment variables, defaults match the Makefile):
#   ARGOCD_NAMESPACE    — namespace for ArgoCD          (default: argocd)
#   KEDA_NAMESPACE      — namespace for KEDA            (default: keda)
#   ROLLOUTS_NAMESPACE  — namespace for Argo Rollouts   (default: argo-rollouts)
#
# Requirements satisfied: 8.1

set -euo pipefail

# --- Configuration -----------------------------------------------------------

ARGOCD_NAMESPACE="${ARGOCD_NAMESPACE:-argocd}"
KEDA_NAMESPACE="${KEDA_NAMESPACE:-keda}"
ROLLOUTS_NAMESPACE="${ROLLOUTS_NAMESPACE:-argo-rollouts}"

# Pinned chart versions for reproducibility.
ARGOCD_CHART_VERSION="7.7.11"
KEDA_CHART_VERSION="2.16.1"
ROLLOUTS_CHART_VERSION="2.39.1"

# Readiness timeout (seconds) per component.
READINESS_TIMEOUT="${READINESS_TIMEOUT:-300}"

# --- Helper functions --------------------------------------------------------

info()    { printf '  [info]    %s\n' "$*"; }
ok()      { printf '  [ok]      %s\n' "$*"; }
err()     { printf '  [error]   %s\n' "$*" >&2; }
waiting() { printf '  [wait]    %s\n' "$*"; }

wait_for_pods() {
  local namespace="$1"
  local label="$2"
  local component="$3"
  local timeout="${4:-${READINESS_TIMEOUT}}"

  waiting "Waiting for ${component} pods to be ready in '${namespace}' (timeout: ${timeout}s)..."

  if ! kubectl wait --namespace "${namespace}" \
       --for=condition=Ready pod \
       --selector="${label}" \
       --timeout="${timeout}s" 2>/dev/null; then
    err "Timed out waiting for ${component} pods in namespace '${namespace}'"
    err "Current pod status:"
    kubectl get pods --namespace "${namespace}" 2>&1 | sed 's/^/    /' >&2
    return 1
  fi

  ok "${component} pods are ready"
}

verify_pods_running() {
  local namespace="$1"
  local component="$2"

  local not_running
  not_running=$(kubectl get pods --namespace "${namespace}" \
    --no-headers 2>/dev/null \
    | grep -v 'Running\|Completed' || true)

  if [[ -n "${not_running}" ]]; then
    err "Some ${component} pods are not in Running state:"
    echo "${not_running}" | sed 's/^/    /' >&2
    return 1
  fi

  local count
  count=$(kubectl get pods --namespace "${namespace}" --no-headers 2>/dev/null | wc -l | tr -d ' ')
  ok "${component}: all ${count} pod(s) running in '${namespace}'"
}

# --- 1. Add Helm repositories -----------------------------------------------

info "Adding Helm repositories..."

helm repo add argo https://argoproj.github.io/argo-helm --force-update >/dev/null 2>&1
helm repo add kedacore https://kedacore.github.io/charts --force-update >/dev/null 2>&1
helm repo update >/dev/null 2>&1

ok "Helm repositories added and updated"

# --- 2. Install ArgoCD -------------------------------------------------------

info "Installing ArgoCD (chart version ${ARGOCD_CHART_VERSION}) into namespace '${ARGOCD_NAMESPACE}'..."

kubectl create namespace "${ARGOCD_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

helm upgrade --install argocd argo/argo-cd \
  --namespace "${ARGOCD_NAMESPACE}" \
  --version "${ARGOCD_CHART_VERSION}" \
  --set 'crds.install=true' \
  --set 'server.service.type=ClusterIP' \
  --set 'configs.params."server\.insecure"=true' \
  --wait \
  --timeout "${READINESS_TIMEOUT}s" \
  >/dev/null

ok "ArgoCD Helm release installed"

wait_for_pods "${ARGOCD_NAMESPACE}" "app.kubernetes.io/part-of=argocd" "ArgoCD"
verify_pods_running "${ARGOCD_NAMESPACE}" "ArgoCD"

# --- 3. Install KEDA ---------------------------------------------------------

info "Installing KEDA (chart version ${KEDA_CHART_VERSION}) into namespace '${KEDA_NAMESPACE}'..."

kubectl create namespace "${KEDA_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

helm upgrade --install keda kedacore/keda \
  --namespace "${KEDA_NAMESPACE}" \
  --version "${KEDA_CHART_VERSION}" \
  --wait \
  --timeout "${READINESS_TIMEOUT}s" \
  >/dev/null

ok "KEDA Helm release installed"

wait_for_pods "${KEDA_NAMESPACE}" "app.kubernetes.io/instance=keda" "KEDA"
verify_pods_running "${KEDA_NAMESPACE}" "KEDA"

# --- 4. Install Argo Rollouts ------------------------------------------------

info "Installing Argo Rollouts (chart version ${ROLLOUTS_CHART_VERSION}) into namespace '${ROLLOUTS_NAMESPACE}'..."

kubectl create namespace "${ROLLOUTS_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

helm upgrade --install argo-rollouts argo/argo-rollouts \
  --namespace "${ROLLOUTS_NAMESPACE}" \
  --version "${ROLLOUTS_CHART_VERSION}" \
  --set 'dashboard.enabled=false' \
  --wait \
  --timeout "${READINESS_TIMEOUT}s" \
  >/dev/null

ok "Argo Rollouts Helm release installed"

wait_for_pods "${ROLLOUTS_NAMESPACE}" "app.kubernetes.io/instance=argo-rollouts" "Argo Rollouts"
verify_pods_running "${ROLLOUTS_NAMESPACE}" "Argo Rollouts"

# --- 5. Summary --------------------------------------------------------------

printf '\n'
printf '  ============================================================\n'
printf '  Platform layer installed successfully.\n'
printf '\n'
printf '    ArgoCD:        %s (namespace: %s)\n' "${ARGOCD_CHART_VERSION}" "${ARGOCD_NAMESPACE}"
printf '    KEDA:          %s (namespace: %s)\n' "${KEDA_CHART_VERSION}" "${KEDA_NAMESPACE}"
printf '    Argo Rollouts: %s (namespace: %s)\n' "${ROLLOUTS_CHART_VERSION}" "${ROLLOUTS_NAMESPACE}"
printf '\n'
printf '  All components ready. ArgoCD now owns the cluster.\n'
printf '  Next step: make argocd-sync\n'
printf '  ============================================================\n'
printf '\n'
