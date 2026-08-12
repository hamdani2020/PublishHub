# Distributed Tracing

How a single publish request becomes one trace across two services and two
languages (Requirement 14.2), and how structured logs correlate with those
traces automatically (Requirement 14.3).

## Trace Propagation: API → Queue → Worker

A publish request produces one distributed trace that spans the API handler and
the worker's processing of the resulting job. The trace context travels through
the message envelope, not through HTTP headers between services (there is no
HTTP call from API to worker — the queue is the transport).

### Flow

```
 Browser / Client
   │
   │  POST /api/v1/publish
   ▼
┌─────────────────────────────────────────────────────────┐
│  API (publishhub-api)                                   │
│                                                         │
│  1. dd-trace creates a span for the HTTP request        │
│  2. tracing.traceHeaders() extracts propagation headers │
│     from the active span: x-datadog-trace-id,           │
│     x-datadog-parent-id, x-datadog-sampling-priority    │
│  3. publish-router puts those headers into the job      │
│     envelope as trace_context: {...}                     │
│  4. Job is enqueued (Redis LPUSH or SQS SendMessage)    │
└─────────────────────────────────────────────────────────┘
   │
   │  Queue (Redis list or SQS)
   │  Message body is JSON with trace_context field
   │
   ▼
┌─────────────────────────────────────────────────────────┐
│  Worker (publishhub-worker)                             │
│                                                         │
│  1. Job loop receives message, parses envelope          │
│  2. tracing.job_span() is called with the envelope's    │
│     trace_context headers                               │
│  3. TracerPort.activate(headers) makes the API's span   │
│     the active parent context                           │
│  4. TracerPort.start_span("publishhub.worker.job")      │
│     creates a child span under that parent              │
│  5. The child span is tagged and finished when the      │
│     job completes or fails                              │
└─────────────────────────────────────────────────────────┘
```

### The Envelope's `trace_context` Field

The `trace_context` field in the message envelope (documented in
[`docs/message-schema.md`](./message-schema.md)) is a flat object mapping
Datadog propagation header names to their string values:

```json
{
  "trace_context": {
    "x-datadog-trace-id": "6249442685991245312",
    "x-datadog-parent-id": "8114249130118331704",
    "x-datadog-sampling-priority": "1"
  }
}
```

When `OBSERVABILITY_ENABLED=false`, the API sends `{}` and the worker starts a
root span (or no span at all, depending on the disabled path). This is
documented behavior, not a degraded mode.

### Key Code Locations

| Concern | API (TypeScript) | Worker (Python) |
|---|---|---|
| Tracer interface | `src/observability/tracing.ts` | `src/publishhub_worker/observability/tracing.py` |
| Header injection | `Tracing.traceHeaders()` | — |
| Header extraction | — | `TracerPort.activate(headers)` |
| Span creation | Automatic (dd-trace patches Express) | `TracerPort.start_span(JOB_SPAN_NAME)` |
| Envelope wiring | `publish-router.ts` → `createPublishJob({trace_context})` | `job_loop.py` → `tracing.job_span(trace_context=job.trace_context)` |

## Log-Trace Correlation

Both services emit structured JSON logs with trace identifiers when a span is
active. This allows navigating from a log line to the trace that produced it,
and from a trace to all the log lines emitted during it.

### Log Fields

When `OBSERVABILITY_ENABLED=true` and a span is active, every log line includes:

```json
{
  "time": "2026-08-07T10:00:00.123Z",
  "level": "info",
  "service": "publishhub-api",
  "env": "production",
  "msg": "publish request queued",
  "dd": {
    "trace_id": "6249442685991245312",
    "span_id": "8114249130118331704"
  }
}
```

The `dd` object matches Datadog's expected log correlation format. Both
services produce the same shape so a single query in Datadog Logs finds lines
from both:

| Field | Meaning |
|---|---|
| `dd.trace_id` | The trace this line belongs to |
| `dd.span_id` | The specific span that was active when the line was emitted |

### How It Works

**API** (`apps/api/src/logging/logger.ts`):
- The logger is created with a `traceContext` mixin function
- On every log line, pino calls the mixin, which calls `tracing.traceContext()`
- When a span is active: returns `{ dd: { trace_id, span_id } }`
- When no span is active: returns `undefined` (no `dd` field on the line)

**Worker** (`apps/worker/src/publishhub_worker/logging/logger.py`):
- `JsonFormatter` accepts a `trace_context` callable
- On every log line, the formatter calls it and merges the result into the JSON
- The callable is `observability.tracing.trace_context`, wired in the entrypoint
- When a span is active: returns `{"dd": {"trace_id": ..., "span_id": ...}}`
- When no span is active: returns `None` (no `dd` field on the line)

### When Tracing Is Off

With `OBSERVABILITY_ENABLED=false`:
- No tracer is loaded, no library is imported
- `traceContext()` / `trace_context()` always returns `undefined` / `None`
- Log lines carry `service` and `env` but no `dd` object
- The output is structured JSON either way — only the trace fields are absent

## Verifying in the Datadog UI

### Single Trace Spanning Both Services

1. Open **APM → Traces** in Datadog
2. Filter: `service:publishhub-api OR service:publishhub-worker`
3. Look for traces with spans from both services
4. Click a trace to open the flame graph:
   - Top-level span: `express.request` (from `publishhub-api`)
   - Child span: `publishhub.worker.job` (from `publishhub-worker`)
5. Both spans share the same `trace_id`; the worker span's `parent_id` points
   to the API's span

### Log Correlation

1. From the trace detail page, click **Logs** in the sidebar
2. Confirm log lines from both services appear, linked by `trace_id`
3. Alternatively, in **Logs**, search: `@dd.trace_id:<the-trace-id>`
4. Lines from both `publishhub-api` and `publishhub-worker` should appear

### Service Map

In **APM → Service Map**, the dependency arrow from `publishhub-api` to
`publishhub-worker` confirms that traces flow between them. The connection is
indirect (through the queue), but the trace propagation makes it visible as a
direct dependency in the map.

## Disabled Path Behavior

| Condition | `trace_context` in envelope | Worker span | Log `dd` fields |
|---|---|---|---|
| `OBSERVABILITY_ENABLED=true` | `{x-datadog-*: "..."}` | Child of API span | Present |
| `OBSERVABILITY_ENABLED=false` | `{}` | No-op (inert) | Absent |

The disabled path is designed to be genuinely inert: no APM library is loaded,
no socket is opened, no module is patched. This is stronger than "tracing is
configured but turned off" — the library never enters the process.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Worker spans appear as root spans (no parent) | `trace_context` is `{}` — check that `OBSERVABILITY_ENABLED=true` is set for the API |
| Log lines have no `dd` field | Tracing is off, or the log line was emitted outside a span (startup, shutdown, idle poll) |
| Two separate traces instead of one | The worker is not calling `activate()` with the envelope's headers — check that `trace_context` is being passed through |
| Trace appears but worker span is missing | Worker has `OBSERVABILITY_ENABLED=false` even though the API has it on |
| `dd-trace` or `ddtrace` import error | The library is not installed — it is a conditional dependency loaded only when enabled |
