#!/usr/bin/env bash
# scripts/load-test.sh
#
# Submit a configurable burst of publish requests against the port-forwarded API.
# Used by the scaling verification script to generate queue depth for KEDA.
#
# Exits 0 on success, non-zero on any failure.
#
# Configuration (environment variables):
#   BURST_COUNT    — number of posts to submit       (default: 50)
#   API_LOCAL_PORT — port-forward local port         (default: 8081)
#   CONCURRENCY    — parallel curl processes         (default: 10)
#   CONTENT_PREFIX — prefix for generated content    (default: "Load test post")
#
# Output:
#   Prints each submitted post id to stdout (one per line).
#   On failure, prints the failing response to stderr.
#
# Requirements satisfied: 9.7

set -euo pipefail

# --- Configuration -----------------------------------------------------------

BURST_COUNT="${BURST_COUNT:-50}"
API_LOCAL_PORT="${API_LOCAL_PORT:-8081}"
CONCURRENCY="${CONCURRENCY:-10}"
CONTENT_PREFIX="${CONTENT_PREFIX:-Load test post}"

API_URL="http://localhost:${API_LOCAL_PORT}"
PUBLISH_ENDPOINT="${API_URL}/api/v1/publish"

# --- Helper functions --------------------------------------------------------

info()    { printf '  [info]    %s\n' "$*" >&2; }
ok()      { printf '  [ok]      %s\n' "$*" >&2; }
err()     { printf '  [error]   %s\n' "$*" >&2; }

# --- Prerequisite checks -----------------------------------------------------

if ! command -v curl &>/dev/null; then
  err "curl is required but not installed"
  exit 1
fi

# Verify the API is reachable before flooding it.
if ! curl --silent --fail --max-time 5 -o /dev/null "${API_URL}/health" 2>/dev/null; then
  err "API is not reachable at ${API_URL}/health — is the port-forward running?"
  exit 1
fi

# --- Burst submission --------------------------------------------------------

info "Submitting ${BURST_COUNT} publish requests to ${PUBLISH_ENDPOINT} (concurrency: ${CONCURRENCY})..."

submitted=0
failed=0
post_ids=()
start_time=$(date +%s)

# Submit posts in batches controlled by CONCURRENCY.
# Each request runs in the background; we wait for each batch before the next.
batch_pids=()
batch_tmpfiles=()

for i in $(seq 1 "${BURST_COUNT}"); do
  tmpfile=$(mktemp)
  batch_tmpfiles+=("${tmpfile}")

  (
    response=$(curl --silent --max-time 15 \
      -w "\n%{http_code}" \
      -X POST "${PUBLISH_ENDPOINT}" \
      -H "Content-Type: application/json" \
      -d "{\"content\": \"${CONTENT_PREFIX} ${i} of ${BURST_COUNT}\", \"platforms\": [\"twitter\", \"linkedin\"]}" 2>/dev/null || echo "")

    echo "${response}" > "${tmpfile}"
  ) &
  batch_pids+=($!)

  # When we hit the concurrency limit, wait for the batch to finish.
  if (( ${#batch_pids[@]} >= CONCURRENCY )); then
    for pid in "${batch_pids[@]}"; do
      wait "${pid}" 2>/dev/null || true
    done
    batch_pids=()
  fi
done

# Wait for any remaining requests.
for pid in "${batch_pids[@]}"; do
  wait "${pid}" 2>/dev/null || true
done

# --- Parse results -----------------------------------------------------------

for tmpfile in "${batch_tmpfiles[@]}"; do
  if [[ ! -f "${tmpfile}" ]]; then
    ((failed++))
    continue
  fi

  response=$(cat "${tmpfile}")
  rm -f "${tmpfile}"

  # The response ends with the HTTP status code on its own line (from -w).
  http_code=$(echo "${response}" | tail -1)
  body=$(echo "${response}" | sed '$d')

  if [[ "${http_code}" == "202" ]]; then
    post_id=$(echo "${body}" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
    if [[ -n "${post_id}" ]]; then
      post_ids+=("${post_id}")
      ((submitted++))
    else
      ((failed++))
      err "Got 202 but could not extract post id from response"
    fi
  else
    ((failed++))
  fi
done

# --- Summary -----------------------------------------------------------------

end_time=$(date +%s)
duration=$((end_time - start_time))

if (( failed > 0 )); then
  info "Warning: ${failed} request(s) failed out of ${BURST_COUNT}"
fi

if (( submitted == 0 )); then
  err "No posts were successfully submitted"
  exit 1
fi

printf '\n' >&2
printf '  ============================================================\n' >&2
printf '  Load generation complete\n' >&2
printf '\n' >&2
printf '    Total requests: %d\n' "${BURST_COUNT}" >&2
printf '    Succeeded:      %d (HTTP 202)\n' "${submitted}" >&2
printf '    Failed:         %d\n' "${failed}" >&2
printf '    Duration:       %ds\n' "${duration}" >&2
printf '    Concurrency:    %d\n' "${CONCURRENCY}" >&2
printf '  ============================================================\n' >&2
printf '\n' >&2

if (( failed > 0 )); then
  err "${failed} request(s) did not return 202 — check the API logs"
  exit 1
fi

ok "All ${submitted} posts accepted (202)"

# Print post ids to stdout (for consumption by calling scripts).
for id in "${post_ids[@]}"; do
  echo "${id}"
done
