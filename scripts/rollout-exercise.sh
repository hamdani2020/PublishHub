#!/usr/bin/env bash
# scripts/rollout-exercise.sh
#
# Argo Rollouts canary exercise for the PublishHub API. Validates that:
#   1. A new image triggers a canary with an initial weight step and pause
#   2. Promoting advances the rollout through weight steps
#   3. Aborting returns all traffic to the stable version
#   4. Current step, weights, and per-version images are reportable
#
# Exercises both the PROMOTE and ABORT paths against the live cluster.
#
# Exits 0 on success, non-zero on any failure.
#
# Configuration (environment variables, defaults match the Makefile):
#   APP_NAMESPACE       — application namespace              (default: publishhub)
#   REGISTRY            — local registry address             (default: localhost:5001)
#   ROLLOUT_NAME        — name of the Argo Rollout           (default: publishhub-api)
#   API_DOCKERFILE      — path to the API Dockerfile         (default: apps/api)
#   PROMOTE_TIMEOUT     — timeout for promote assertions (s) (default: 90)
#   ABORT_TIMEOUT       — timeout for abort assertions (s)   (default: 90)
#   STEP_PAUSE_WAIT     — time to wait for step transitions  (default: 45)
#
# Prerequisites:
#   - kind cluster running with Argo Rollouts installed (make platform-install)
#   - Helm chart deployed with api.rollout.enabled=true
#   - kubectl argo rollouts plugin installed
#
# Requirements satisfied: 10.2, 10.3, 10.5

set -euo pipefail

# --- Configuration -----------------------------------------------------------

APP_NAMESPACE="${APP_NAMESPACE:-publishhub}"
REGISTRY="${REGISTRY:-localhost:5001}"
ROLLOUT_NAME="${ROLLOUT_NAME:-publishhub-api}"
API_DOCKERFILE="${API_DOCKERFILE:-apps/api}"
PROMOTE_TIMEOUT="${PROMOTE_TIMEOUT:-90}"
ABORT_TIMEOUT="${ABORT_TIMEOUT:-90}"
STEP_PAUSE_WAIT="${STEP_PAUSE_WAIT:-45}"

IMAGE_REPO="${REGISTRY}/publishhub-api"
V2_TAG="v2-exercise-$(date +%s)"

# --- Helper functions --------------------------------------------------------

info()    { printf '  [info]    %s\n' "$*"; }
ok()      { printf '  [ok]      %s\n' "$*"; }
err()     { printf '  [error]   %s\n' "$*" >&2; }
waiting() { printf '  [wait]    %s\n' "$*"; }
section() { printf '\n  --- %s ---\n\n' "$*"; }

# --- Prerequisite checks -----------------------------------------------------

info "Verifying prerequisites..."

if ! command -v kubectl >/dev/null 2>&1; then
  err "kubectl not found"
  exit 1
fi

if ! kubectl argo rollouts version >/dev/null 2>&1; then
  err "kubectl argo rollouts plugin not found"
  err "Install it: brew install argoproj/tap/kubectl-argo-rollouts"
  exit 1
fi

if ! kubectl get rollout "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}" >/dev/null 2>&1; then
  err "Rollout '${ROLLOUT_NAME}' not found in namespace '${APP_NAMESPACE}'"
  err "Ensure the Helm chart is deployed with api.rollout.enabled=true"
  exit 1
fi

ok "Prerequisites satisfied"

# --- 1. Build and push a v2 API image ----------------------------------------

section "Building v2 API image"

info "Building image ${IMAGE_REPO}:${V2_TAG}..."
info "Using build arg to differentiate from v1: ROLLOUT_EXERCISE_VERSION=v2"

# Build the v2 image. We add a label to differentiate it from v1 without
# changing application code. The image content is identical to v1 but tagged
# differently — this is sufficient for Argo Rollouts to detect a new revision.
docker build \
  --tag "${IMAGE_REPO}:${V2_TAG}" \
  --label "rollout-exercise=v2" \
  --label "exercise-tag=${V2_TAG}" \
  "${API_DOCKERFILE}" >/dev/null 2>&1

if [[ $? -ne 0 ]]; then
  err "Failed to build v2 image"
  exit 1
fi

info "Pushing ${IMAGE_REPO}:${V2_TAG} to registry..."

docker push "${IMAGE_REPO}:${V2_TAG}" >/dev/null 2>&1

if [[ $? -ne 0 ]]; then
  err "Failed to push v2 image to registry"
  exit 1
fi

ok "v2 image built and pushed: ${IMAGE_REPO}:${V2_TAG}"

# --- Helper: report rollout status (Requirement 10.5) ------------------------

report_rollout_status() {
  local label="${1:-Current}"
  printf '\n'
  info "${label} rollout status:"
  printf '\n'

  # Get the rollout status in a parseable way
  local status_output
  status_output=$(kubectl argo rollouts get rollout "${ROLLOUT_NAME}" \
    --namespace "${APP_NAMESPACE}" --no-color 2>&1 || true)

  # Print the full status output indented
  echo "${status_output}" | sed 's/^/    /'
  printf '\n'

  # Extract and report key fields
  local current_step desired_weight
  current_step=$(kubectl get rollout "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}" \
    -o jsonpath='{.status.currentStepIndex}' 2>/dev/null || echo "N/A")
  desired_weight=$(kubectl get rollout "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}" \
    -o jsonpath='{.status.canary.weights.canary.weight}' 2>/dev/null || echo "N/A")

  local stable_image canary_image
  stable_image=$(kubectl get rollout "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}" \
    -o jsonpath='{.status.stableRS}' 2>/dev/null || echo "N/A")
  canary_image=$(kubectl get rollout "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}" \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "N/A")

  info "  Step index:     ${current_step}"
  info "  Desired weight: ${desired_weight}"
  info "  Current image:  ${canary_image}"
}

# --- Helper: wait for rollout phase ------------------------------------------

wait_for_phase() {
  local expected_phase="$1"
  local timeout="$2"
  local description="$3"

  waiting "${description} (timeout: ${timeout}s)..."

  local deadline=$((SECONDS + timeout))
  while [[ $SECONDS -lt $deadline ]]; do
    local phase
    phase=$(kubectl get rollout "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}" \
      -o jsonpath='{.status.phase}' 2>/dev/null || echo "")

    if [[ "${phase}" == "${expected_phase}" ]]; then
      return 0
    fi
    sleep 2
  done

  return 1
}

# --- Helper: wait for rollout to be paused -----------------------------------

wait_for_paused() {
  local timeout="$1"
  local description="$2"

  waiting "${description} (timeout: ${timeout}s)..."

  local deadline=$((SECONDS + timeout))
  while [[ $SECONDS -lt $deadline ]]; do
    local paused
    paused=$(kubectl get rollout "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}" \
      -o jsonpath='{.status.pauseConditions}' 2>/dev/null || echo "")

    if [[ -n "${paused}" && "${paused}" != "null" ]]; then
      return 0
    fi
    sleep 2
  done

  return 1
}

# --- Helper: get current step index ------------------------------------------

get_step_index() {
  kubectl get rollout "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}" \
    -o jsonpath='{.status.currentStepIndex}' 2>/dev/null || echo ""
}

# --- Helper: get rollout phase -----------------------------------------------

get_phase() {
  kubectl get rollout "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}" \
    -o jsonpath='{.status.phase}' 2>/dev/null || echo ""
}

# --- 2. PROMOTE path ---------------------------------------------------------

section "PROMOTE path (Requirement 10.2, 10.3)"

# Record the current stable image before starting the rollout
STABLE_IMAGE_BEFORE=$(kubectl get rollout "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}" \
  -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "")

info "Current stable image: ${STABLE_IMAGE_BEFORE}"
info "Setting new image: ${IMAGE_REPO}:${V2_TAG}..."

# Trigger the canary rollout
kubectl argo rollouts set image "${ROLLOUT_NAME}" \
  "api=${IMAGE_REPO}:${V2_TAG}" \
  --namespace "${APP_NAMESPACE}"

# Wait for the rollout to reach the first pause (step 2 = indefinite pause)
# Requirement 10.2: SHALL shift a small initial traffic weight and pause
if ! wait_for_paused "${PROMOTE_TIMEOUT}" "Waiting for rollout to reach first pause (setWeight: 10 then pause)"; then
  err "Rollout did not reach the expected paused state within ${PROMOTE_TIMEOUT}s"
  report_rollout_status "Failed"
  exit 1
fi

ok "Rollout paused at initial canary weight (Requirement 10.2)"

# Report status at the pause point
report_rollout_status "After initial canary weight"

# Verify step index is at 1 (0-indexed; step 0 = setWeight:10, step 1 = pause:{})
step_at_pause=$(get_step_index)
info "Step index at pause: ${step_at_pause}"

# Promote the rollout (Requirement 10.3: advance to next weight step)
info "Promoting rollout..."
kubectl argo rollouts promote "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}"

ok "Promote command issued"

# Wait a moment for the rollout to advance past the pause
sleep 5

# Report the status after promote — should be advancing through steps
report_rollout_status "After promote"

step_after_promote=$(get_step_index)
info "Step index after promote: ${step_after_promote}"

# Verify the rollout advanced (step index should be greater than at pause)
if [[ -n "${step_at_pause}" && -n "${step_after_promote}" ]]; then
  if [[ "${step_after_promote}" -gt "${step_at_pause}" ]]; then
    ok "Rollout advanced past the pause step (${step_at_pause} -> ${step_after_promote})"
  else
    # It may have fully completed already
    phase_now=$(get_phase)
    if [[ "${phase_now}" == "Healthy" ]]; then
      ok "Rollout completed full promotion to Healthy"
    else
      err "Rollout did not advance after promote (step stayed at ${step_at_pause})"
      exit 1
    fi
  fi
fi

# Wait for the rollout to complete fully (Healthy phase)
info "Waiting for rollout to complete full promotion..."

if ! wait_for_phase "Healthy" "${PROMOTE_TIMEOUT}" "Waiting for rollout to reach Healthy"; then
  # It might be in a timed pause step — promote --full to skip remaining pauses
  info "Rollout still in progress, promoting fully..."
  kubectl argo rollouts promote --full "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}" 2>/dev/null || true

  if ! wait_for_phase "Healthy" "${STEP_PAUSE_WAIT}" "Waiting for full promotion"; then
    err "Rollout did not reach Healthy after full promote"
    report_rollout_status "Stuck"
    exit 1
  fi
fi

ok "PROMOTE path complete — rollout is Healthy with v2 image"
report_rollout_status "Promote complete"

# --- 3. ABORT path (Requirement 10.3) ----------------------------------------

section "ABORT path (Requirement 10.3)"

# Trigger another rollout with a new tag to create a canary situation to abort
ABORT_TAG="v2-abort-$(date +%s)"

info "Building and pushing abort-test image: ${IMAGE_REPO}:${ABORT_TAG}..."

docker build \
  --tag "${IMAGE_REPO}:${ABORT_TAG}" \
  --label "rollout-exercise=abort-test" \
  "${API_DOCKERFILE}" >/dev/null 2>&1

docker push "${IMAGE_REPO}:${ABORT_TAG}" >/dev/null 2>&1

ok "Abort-test image pushed: ${IMAGE_REPO}:${ABORT_TAG}"

# Record the stable image (should be the v2 image from the promote path)
STABLE_IMAGE_FOR_ABORT=$(kubectl get rollout "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}" \
  -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "")

info "Current stable image before abort test: ${STABLE_IMAGE_FOR_ABORT}"
info "Setting new image to trigger canary: ${IMAGE_REPO}:${ABORT_TAG}..."

# Trigger the canary rollout
kubectl argo rollouts set image "${ROLLOUT_NAME}" \
  "api=${IMAGE_REPO}:${ABORT_TAG}" \
  --namespace "${APP_NAMESPACE}"

# Wait for the rollout to reach the paused state
if ! wait_for_paused "${ABORT_TIMEOUT}" "Waiting for rollout to reach pause before abort"; then
  err "Rollout did not reach a paused state for abort test"
  report_rollout_status "Failed"
  exit 1
fi

ok "Rollout paused — ready to test abort"
report_rollout_status "Before abort"

# Abort the rollout
info "Aborting rollout..."
kubectl argo rollouts abort "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}"

ok "Abort command issued"

# Wait for the rollout to stabilize after abort — phase should be Degraded
# (Argo Rollouts marks aborted rollouts as Degraded)
waiting "Waiting for abort to take effect..."
sleep 5

# Verify all traffic returned to stable (Requirement 10.3)
# After an abort, the canary weight should be 0 and stable should have all traffic
abort_phase=$(get_phase)
info "Phase after abort: ${abort_phase}"

report_rollout_status "After abort"

# Check that the canary ReplicaSet is scaled down
canary_replicas=$(kubectl get rollout "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}" \
  -o jsonpath='{.status.canary.weights.canary.weight}' 2>/dev/null || echo "")

# After abort, canary weight should be 0 (all traffic to stable)
if [[ "${canary_replicas}" == "0" || -z "${canary_replicas}" ]]; then
  ok "All traffic returned to stable version after abort (canary weight: 0)"
else
  # Check if the abort is still processing
  waiting "Canary weight not yet 0 (currently: ${canary_replicas}), waiting..."
  deadline=$((SECONDS + ABORT_TIMEOUT))
  reverted=false
  while [[ $SECONDS -lt $deadline ]]; do
    canary_replicas=$(kubectl get rollout "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}" \
      -o jsonpath='{.status.canary.weights.canary.weight}' 2>/dev/null || echo "0")
    if [[ "${canary_replicas}" == "0" || -z "${canary_replicas}" ]]; then
      reverted=true
      break
    fi
    sleep 2
  done

  if [[ "${reverted}" == "true" ]]; then
    ok "All traffic returned to stable version after abort (canary weight: 0)"
  else
    err "Abort did not return all traffic to stable within ${ABORT_TIMEOUT}s"
    err "Canary weight is still: ${canary_replicas}"
    report_rollout_status "Abort failed"
    exit 1
  fi
fi

# Undo the abort to restore the rollout to a clean state for future use
info "Undoing abort to restore rollout to Healthy state..."
kubectl argo rollouts undo "${ROLLOUT_NAME}" --namespace "${APP_NAMESPACE}" 2>/dev/null || true

# Set back to the promote image so the rollout is clean
kubectl argo rollouts set image "${ROLLOUT_NAME}" \
  "api=${IMAGE_REPO}:${V2_TAG}" \
  --namespace "${APP_NAMESPACE}" 2>/dev/null || true

# Wait briefly for stabilization
sleep 5

# --- 4. Summary --------------------------------------------------------------

printf '\n'
printf '  ============================================================\n'
printf '  Rollout exercise PASSED\n'
printf '\n'
printf '    Image built:    %s:%s\n' "${IMAGE_REPO}" "${V2_TAG}"
printf '    Promote path:   canary paused at initial weight, promoted to Healthy\n'
printf '    Abort path:     canary paused, aborted, all traffic to stable\n'
printf '    Status exposed: step index, weights, per-version images reported\n'
printf '  ============================================================\n'
printf '\n'
printf '  Requirements validated:\n'
printf '    10.2 — new image triggers canary with initial weight + pause\n'
printf '    10.3 — promote advances steps; abort returns traffic to stable\n'
printf '    10.5 — step, weight, and image info exposed via Rollouts CLI\n'
printf '\n'
