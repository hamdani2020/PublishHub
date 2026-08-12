#!/usr/bin/env bash
# scripts/verify-tracing.sh
#
# Verification script for distributed tracing across the API and worker services.
# Validates Requirements 14.2 (distributed trace propagation) and 14.3 (log-trace
# correlation).
#
# This script performs two kinds of verification:
#
#   1. STATIC CODE-PATH VERIFICATION (runs without infrastructure)
#      Checks that the code paths for trace propagation exist and are wired:
#      - The API produces traceHeaders() that go into the job envelope
#      - The worker reads trace_context from the envelope and activates the parent
#      - Both loggers include trace identifiers in structured output
#
#   2. MANUAL RUNTIME VERIFICATION (documented procedure, requires Datadog agent)
#      Prints the procedure an operator follows with OBSERVABILITY_ENABLED=true
#      to confirm a single trace spans both services in the Datadog UI.
#
# Exit codes:
#   0 — all static verifications pass
#   1 — a verification failed (missing code path or broken wiring)
#
# Requirements satisfied: 14.2, 14.3

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- Helper functions --------------------------------------------------------

pass()  { printf '  [\033[32mPASS\033[0m]  %s\n' "$*"; }
fail()  { printf '  [\033[31mFAIL\033[0m]  %s\n' "$*" >&2; }
info()  { printf '  [info]  %s\n' "$*"; }
header(){ printf '\n  === %s ===\n\n' "$*"; }

failures=0

check() {
  local description="$1"
  local file="$2"
  local pattern="$3"

  if grep -qE "${pattern}" "${REPO_ROOT}/${file}" 2>/dev/null; then
    pass "${description}"
  else
    fail "${description}"
    fail "  expected pattern '${pattern}' in ${file}"
    ((failures++))
  fi
}

check_file_exists() {
  local description="$1"
  local file="$2"

  if [[ -f "${REPO_ROOT}/${file}" ]]; then
    pass "${description}"
  else
    fail "${description}"
    fail "  file not found: ${file}"
    ((failures++))
  fi
}

# =============================================================================
# PART 1: STATIC CODE-PATH VERIFICATION
# =============================================================================

header "Static Verification: Distributed Trace Propagation (Req 14.2)"

# --- 1.1 API produces traceHeaders() ----------------------------------------

info "Checking API tracing module produces propagation headers..."

check \
  "API tracing.ts defines traceHeaders() method" \
  "apps/api/src/observability/tracing.ts" \
  "traceHeaders\(\).*Record<string, string>"

check \
  "API tracing.ts injects context via tracer.inject()" \
  "apps/api/src/observability/tracing.ts" \
  "tracer\.inject\(context.*HTTP_HEADERS_FORMAT.*carrier\)"

check \
  "API tracing.ts exports HTTP_HEADERS_FORMAT constant" \
  "apps/api/src/observability/tracing.ts" \
  "export const HTTP_HEADERS_FORMAT"

# --- 1.2 Publish router puts headers into the envelope -----------------------

info "Checking publish router injects trace headers into job envelope..."

check \
  "Publish router calls traceHeaders() for trace_context" \
  "apps/api/src/posts/publish-router.ts" \
  "trace_context.*traceHeaders\(\)"

check \
  "Publish router declares traceHeaders dependency" \
  "apps/api/src/posts/publish-router.ts" \
  "traceHeaders.*\(\).*Record<string, string>"

# --- 1.3 App wires tracing.traceHeaders to publish router --------------------

info "Checking app.ts wires tracing into publish router..."

check \
  "app.ts passes tracing.traceHeaders() to publish router" \
  "apps/api/src/app.ts" \
  "traceHeaders.*tracing\.traceHeaders"

# --- 1.4 Worker extracts trace_context and activates parent span -------------

info "Checking worker tracing reads trace_context from envelope..."

check \
  "Worker tracing.py defines job_span method" \
  "apps/worker/src/publishhub_worker/observability/tracing.py" \
  "def job_span"

check \
  "Worker tracing.py accepts trace_context parameter" \
  "apps/worker/src/publishhub_worker/observability/tracing.py" \
  "trace_context: Mapping\[str, str\]"

check \
  "Worker tracing.py calls port.activate(trace_context)" \
  "apps/worker/src/publishhub_worker/observability/tracing.py" \
  "self\._port\.activate\(trace_context\)"

check \
  "Worker tracing.py starts a child span with JOB_SPAN_NAME" \
  "apps/worker/src/publishhub_worker/observability/tracing.py" \
  "self\._port\.start_span\(JOB_SPAN_NAME"

# --- 1.5 Job loop passes envelope's trace_context to job_span ----------------

info "Checking job loop passes trace_context from the envelope..."

check \
  "Job loop passes job.trace_context to tracing.job_span()" \
  "apps/worker/src/publishhub_worker/processing/job_loop.py" \
  "trace_context=.*job\.trace_context"

# --- 1.6 Message schema includes trace_context field -------------------------

info "Checking message schema defines trace_context..."

check \
  "Message schema documents trace_context field" \
  "docs/message-schema.md" \
  "trace_context.*Datadog distributed-trace propagation headers"

header "Static Verification: Log-Trace Correlation (Req 14.3)"

# --- 2.1 API logger includes trace identifiers ------------------------------

info "Checking API logger merges trace context into log lines..."

check \
  "API logger.ts accepts TraceContextProvider" \
  "apps/api/src/logging/logger.ts" \
  "TraceContextProvider"

check \
  "API logger.ts uses mixin for trace context injection" \
  "apps/api/src/logging/logger.ts" \
  "mixin.*traceContext"

check \
  "API tracing.ts produces dd.trace_id and dd.span_id" \
  "apps/api/src/observability/tracing.ts" \
  "dd.*trace_id.*toTraceId\(\).*span_id.*toSpanId\(\)"

# --- 2.2 Worker logger includes trace identifiers ---------------------------

info "Checking worker logger merges trace context into log lines..."

check \
  "Worker logger.py accepts trace_context provider" \
  "apps/worker/src/publishhub_worker/logging/logger.py" \
  "trace_context.*TraceContextProvider"

check \
  "Worker logger.py calls trace_context provider per log line" \
  "apps/worker/src/publishhub_worker/logging/logger.py" \
  "self\._trace_context"

check \
  "Worker tracing.py produces dd.trace_id and dd.span_id for log correlation" \
  "apps/worker/src/publishhub_worker/observability/tracing.py" \
  "\"dd\".*dict\(correlation\)"

# --- 2.3 Worker entrypoint wires trace_context provider into logger ----------

info "Checking worker entrypoint wires tracing into logger..."

check \
  "Worker entrypoint connects observability.tracing.trace_context to logger" \
  "apps/worker/src/publishhub_worker/runtime/entrypoint.py" \
  "trace_context=observability\.tracing\.trace_context"

# --- 2.4 API bootstrap wires tracing into the logger -----------------------

check_file_exists \
  "API bootstrap.ts exists and initializes tracing" \
  "apps/api/src/observability/bootstrap.ts"

check \
  "API bootstrap creates process-wide tracing seam" \
  "apps/api/src/observability/bootstrap.ts" \
  "export const tracing.*createTracing"

# =============================================================================
# RESULTS
# =============================================================================

header "Results"

if [[ ${failures} -eq 0 ]]; then
  pass "All static verifications passed"
  printf '\n'
  info "Distributed trace propagation code paths are intact."
  info "The API injects Datadog headers into the job envelope's trace_context,"
  info "and the worker continues the trace by activating those headers as parent."
  info "Both loggers inject dd.trace_id and dd.span_id into every structured log line."
else
  fail "${failures} verification(s) failed"
  printf '\n'
  exit 1
fi

# =============================================================================
# PART 2: MANUAL RUNTIME VERIFICATION PROCEDURE
# =============================================================================

header "Manual Runtime Verification Procedure"

cat << 'EOF'
  The following procedure verifies end-to-end distributed tracing with a live
  Datadog agent. It requires OBSERVABILITY_ENABLED=true and a reachable agent.

  Prerequisites:
    - Datadog agent running (local or in-cluster)
    - OBSERVABILITY_ENABLED=true set for both API and worker
    - DD_ENV, DD_SERVICE, DD_VERSION set (or using defaults)

  Steps:

  1. Start both services with tracing enabled:

       OBSERVABILITY_ENABLED=true DD_ENV=staging make dev
       # or in Kubernetes with values-production.yaml applied

  2. Submit a publish request:

       curl -X POST http://localhost:8080/api/v1/publish \
         -H "Content-Type: application/json" \
         -d '{"content": "Trace verification post", "platforms": ["twitter"]}'

     Note the returned post ID (e.g., post_01HZX3QK7M9V4TDR8N2C5EAB6F).

  3. Wait for the worker to process the job (2-3 seconds with default latency).

  4. Verify in Datadog APM (Traces page):

     a. Filter by service: publishhub-api OR publishhub-worker
     b. Find the trace that contains both:
        - A span from publishhub-api (the HTTP POST handler)
        - A child span from publishhub-worker (publishhub.worker.job)
     c. Confirm both spans share the same trace_id
     d. Confirm the worker span's parent_id matches the API span's span_id

  5. Verify log-trace correlation:

     a. In Datadog Logs, filter by the trace_id from step 4
     b. Confirm both API and worker log lines appear
     c. Each line should carry:
        - dd.trace_id — matches the trace
        - dd.span_id — matches the span that emitted the line
        - service — publishhub-api or publishhub-worker
        - env — matches DD_ENV

  6. Verify the disabled path:

     a. Set OBSERVABILITY_ENABLED=false (or unset it)
     b. Restart both services
     c. Submit another post
     d. Confirm log lines do NOT carry dd.trace_id or dd.span_id
     e. Confirm the job envelope's trace_context is {} (empty object)

  Expected behavior summary:

    | Condition               | API trace_context     | Worker span        | Log fields      |
    |-------------------------|-----------------------|--------------------|-----------------|
    | OBSERVABILITY_ENABLED=1 | {x-datadog-*: "..."}  | child of API span  | dd.trace_id set |
    | OBSERVABILITY_ENABLED=0 | {}                    | root span (no-op)  | no dd.* fields  |

  If steps 4-5 succeed, Requirement 14.2 (distributed trace) and
  Requirement 14.3 (log-trace correlation) are satisfied at runtime.

EOF

printf '  Script complete.\n\n'
