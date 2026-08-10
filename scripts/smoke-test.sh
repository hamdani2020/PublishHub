#!/usr/bin/env bash
# scripts/smoke-test.sh
#
# Post-deploy smoke test for the PublishHub platform. Validates that:
#   1. All pods in the publishhub namespace are Ready
#   2. The KEDA ScaledObject reports Ready
#   3. /health returns 200
#   4. A submitted post reaches a terminal status (completed or failed)
#   5. ArgoCD self-healing restores a mutated resource
#
# Exits 0 on success, non-zero on any failure.
#
# Configuration (environment variables, defaults match the Makefile):
#   APP_NAMESPACE    — application namespace        (default: publishhub)
#   ARGOCD_NAMESPACE — ArgoCD namespace             (default: argocd)
#   API_LOCAL_PORT   — port-forward local port      (default: 8081)
#   POLL_TIMEOUT     — post status poll timeout     (default: 60 seconds)
#   HEAL_TIMEOUT     — self-heal detection timeout  (default: 60 seconds)
#
# Requirements satisfied: 8.5, 9.1

set -euo pipefail

# --- Configuration -----------------------------------------------------------

APP_NAMESPACE="${APP_NAMESPACE:-publishhub}"
ARGOCD_NAMESPACE="${ARGOCD_NAMESPACE:-argocd}"
API_LOCAL_PORT="${API_LOCAL_PORT:-8081}"
POLL_TIMEOUT="${POLL_TIMEOUT:-60}"
HEAL_TIMEOUT="${HEAL_TIMEOUT:-60}"

API_SERVICE="svc/publishhub-api"
API_SERVICE_PORT="8080"
SCALEDOBJECT_NAME="publishhub-worker"
API_DEPLOYMENT="publishhub-api"

# Temporary label used for the self-heal mutation test.
SMOKE_LABEL_KEY="smoke-test-mutation"
SMOKE_LABEL_VALUE="should-be-reverted"

# --- Helper functions --------------------------------------------------------

info()    { printf '  [info]    %s\n' "$*"; }
ok()      { printf '  [ok]      %s\n' "$*"; }
err()     { printf '  [error]   %s\n' "$*" >&2; }
waiting() { printf '  [wait]    %s\n' "$*"; }

# Track the port-forward PID for cleanup.
PF_PID=""

cleanup() {
  if [[ -n "${PF_PID}" ]]; then
    kill "${PF_PID}" 2>/dev/null || true
    wait "${PF_PID}" 2>/dev/null || true
    PF_PID=""
  fi
}

trap cleanup EXIT

# --- 1. Pod readiness --------------------------------------------------------

info "Checking all pods in '${APP_NAMESPACE}' are Ready..."

if ! kubectl get pods --namespace "${APP_NAMESPACE}" --no-headers 2>/dev/null | grep -q .; then
  err "No pods found in namespace '${APP_NAMESPACE}'"
  exit 1
fi

not_ready=$(kubectl get pods --namespace "${APP_NAMESPACE}" \
  --no-headers 2>/dev/null \
  | grep -v 'Running\|Completed' || true)

if [[ -n "${not_ready}" ]]; then
  err "Some pods are not Ready:"
  echo "${not_ready}" | sed 's/^/    /' >&2
  exit 1
fi

# Double-check with a condition-based wait (catches Running but not yet Ready)
if ! kubectl wait --namespace "${APP_NAMESPACE}" \
     --for=condition=Ready pod --all \
     --timeout=30s 2>/dev/null; then
  err "kubectl wait --for=condition=Ready failed for pods in '${APP_NAMESPACE}'"
  kubectl get pods --namespace "${APP_NAMESPACE}" >&2
  exit 1
fi

pod_count=$(kubectl get pods --namespace "${APP_NAMESPACE}" --no-headers 2>/dev/null | wc -l | tr -d ' ')
ok "All ${pod_count} pod(s) in '${APP_NAMESPACE}' are Ready"

# --- 2. ScaledObject readiness (Requirement 9.1) -----------------------------

info "Checking ScaledObject '${SCALEDOBJECT_NAME}' is Ready..."

if ! kubectl get scaledobject "${SCALEDOBJECT_NAME}" --namespace "${APP_NAMESPACE}" >/dev/null 2>&1; then
  err "ScaledObject '${SCALEDOBJECT_NAME}' not found in namespace '${APP_NAMESPACE}'"
  exit 1
fi

# KEDA sets .status.conditions with type=Ready and status=True when active.
so_ready=$(kubectl get scaledobject "${SCALEDOBJECT_NAME}" \
  --namespace "${APP_NAMESPACE}" \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "")

if [[ "${so_ready}" != "True" ]]; then
  # Give KEDA a moment — poll for up to 30 seconds.
  waiting "ScaledObject not yet Ready (status: '${so_ready}'), polling..."
  deadline=$((SECONDS + 30))
  while [[ $SECONDS -lt $deadline ]]; do
    so_ready=$(kubectl get scaledobject "${SCALEDOBJECT_NAME}" \
      --namespace "${APP_NAMESPACE}" \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "")
    if [[ "${so_ready}" == "True" ]]; then
      break
    fi
    sleep 2
  done

  if [[ "${so_ready}" != "True" ]]; then
    err "ScaledObject '${SCALEDOBJECT_NAME}' is not Ready after 30s (status: '${so_ready}')"
    kubectl describe scaledobject "${SCALEDOBJECT_NAME}" --namespace "${APP_NAMESPACE}" >&2
    exit 1
  fi
fi

ok "ScaledObject '${SCALEDOBJECT_NAME}' is Ready"

# --- 3. Port-forward to the API ---------------------------------------------

info "Starting port-forward to ${API_SERVICE} (localhost:${API_LOCAL_PORT} -> ${API_SERVICE_PORT})..."

kubectl port-forward --namespace "${APP_NAMESPACE}" \
  "${API_SERVICE}" "${API_LOCAL_PORT}:${API_SERVICE_PORT}" >/dev/null 2>&1 &
PF_PID=$!

# Wait for the port-forward to be usable (retry a few times).
pf_ready=false
for i in $(seq 1 15); do
  if curl --silent --fail --max-time 2 -o /dev/null "http://localhost:${API_LOCAL_PORT}/health" 2>/dev/null; then
    pf_ready=true
    break
  fi
  sleep 1
done

if [[ "${pf_ready}" != "true" ]]; then
  err "Port-forward did not become ready within 15 seconds"
  exit 1
fi

ok "Port-forward established"

# --- 4. Health endpoint (Requirement 8.5 — service is alive) -----------------

info "Asserting GET /health returns 200..."

health_status=$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --max-time 5 "http://localhost:${API_LOCAL_PORT}/health")

if [[ "${health_status}" != "200" ]]; then
  err "/health returned HTTP ${health_status}, expected 200"
  exit 1
fi

ok "/health returned 200"

# --- 5. Publish a post and poll for terminal status --------------------------

info "Submitting a publish request..."

publish_response=$(curl --silent --max-time 10 \
  -X POST "http://localhost:${API_LOCAL_PORT}/api/v1/publish" \
  -H "Content-Type: application/json" \
  -d '{"content": "Smoke test post from scripts/smoke-test.sh", "platforms": ["twitter"]}')

# Extract the post ID from the response.
post_id=$(echo "${publish_response}" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)

if [[ -z "${post_id}" ]]; then
  err "Failed to extract post ID from publish response:"
  echo "  ${publish_response}" >&2
  exit 1
fi

info "Post submitted: id=${post_id}"
waiting "Polling for terminal status (timeout: ${POLL_TIMEOUT}s)..."

deadline=$((SECONDS + POLL_TIMEOUT))
terminal_status=""

while [[ $SECONDS -lt $deadline ]]; do
  post_response=$(curl --silent --max-time 5 \
    "http://localhost:${API_LOCAL_PORT}/api/v1/posts/${post_id}" 2>/dev/null || echo "")

  current_status=$(echo "${post_response}" | grep -o '"status":"[^"]*"' | head -1 | cut -d'"' -f4)

  if [[ "${current_status}" == "completed" || "${current_status}" == "failed" ]]; then
    terminal_status="${current_status}"
    break
  fi

  sleep 2
done

if [[ -z "${terminal_status}" ]]; then
  err "Post ${post_id} did not reach a terminal status within ${POLL_TIMEOUT}s"
  err "Last response: ${post_response}"
  exit 1
fi

ok "Post ${post_id} reached terminal status: ${terminal_status}"

# --- 6. Self-healing test (Requirement 8.5) ----------------------------------
#
# Mutate a live resource by adding a label directly via kubectl. ArgoCD should
# detect the drift and revert it. We verify by checking the label disappears.

info "Testing ArgoCD self-healing (Requirement 8.5)..."
info "Mutating Deployment '${API_DEPLOYMENT}' with label ${SMOKE_LABEL_KEY}=${SMOKE_LABEL_VALUE}..."

kubectl label deployment "${API_DEPLOYMENT}" \
  --namespace "${APP_NAMESPACE}" \
  "${SMOKE_LABEL_KEY}=${SMOKE_LABEL_VALUE}" \
  --overwrite >/dev/null 2>&1

# Confirm the label was applied.
applied_label=$(kubectl get deployment "${API_DEPLOYMENT}" \
  --namespace "${APP_NAMESPACE}" \
  -o jsonpath="{.metadata.labels.${SMOKE_LABEL_KEY}}" 2>/dev/null || echo "")

if [[ "${applied_label}" != "${SMOKE_LABEL_VALUE}" ]]; then
  err "Failed to apply mutation label to deployment"
  exit 1
fi

info "Label applied. Waiting for ArgoCD to detect drift and self-heal..."

deadline=$((SECONDS + HEAL_TIMEOUT))
healed=false

while [[ $SECONDS -lt $deadline ]]; do
  current_label=$(kubectl get deployment "${API_DEPLOYMENT}" \
    --namespace "${APP_NAMESPACE}" \
    -o jsonpath="{.metadata.labels.${SMOKE_LABEL_KEY}}" 2>/dev/null || echo "")

  if [[ -z "${current_label}" ]]; then
    healed=true
    break
  fi

  sleep 3
done

if [[ "${healed}" != "true" ]]; then
  err "ArgoCD did not revert the mutation within ${HEAL_TIMEOUT}s"
  err "The label '${SMOKE_LABEL_KEY}' is still present on deployment '${API_DEPLOYMENT}'"

  # Show ArgoCD Application status for debugging.
  info "ArgoCD Application status:"
  kubectl get application publishhub --namespace "${ARGOCD_NAMESPACE}" \
    -o jsonpath='{.status.sync.status}' 2>/dev/null | sed 's/^/    /' >&2
  echo "" >&2

  exit 1
fi

ok "ArgoCD self-healed: mutation label removed from '${API_DEPLOYMENT}'"

# --- 7. Summary --------------------------------------------------------------

printf '\n'
printf '  ============================================================\n'
printf '  Smoke test PASSED\n'
printf '\n'
printf '    Pods:         %s ready in %s\n' "${pod_count}" "${APP_NAMESPACE}"
printf '    ScaledObject: %s is Ready\n' "${SCALEDOBJECT_NAME}"
printf '    Health:       /health returned 200\n'
printf '    Publish:      post %s → %s\n' "${post_id}" "${terminal_status}"
printf '    Self-heal:    mutation reverted by ArgoCD\n'
printf '  ============================================================\n'
printf '\n'
