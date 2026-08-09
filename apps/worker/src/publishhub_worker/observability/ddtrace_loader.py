"""
The only module in the worker that names `ddtrace`.

Kept to one adapter so that "is the APM library in this process" has exactly one
answer: it is there if and only if `load_ddtrace_port` was called, which only
happens when `OBSERVABILITY_ENABLED` is true (Requirement 14.6). Everything else in
`observability/` talks to the `TracerPort` protocol and is tested against the
fake in `observability/testing/fake_tracer.py`.

The import is inside the function, not at module scope, so that importing this
module is free and only *calling* it loads eight megabytes of tracer. The call has
to happen before `redis` and `botocore` are imported, which is why
`observability/bootstrap.py` runs at the top of the process entrypoint and why the
worker's Redis and SQS clients import their libraries lazily.

Three pieces of `ddtrace` are used, all of them documented public API in the 4.x
line:

- `ddtrace.trace.tracer` — the global tracer, for starting and finding spans.
- `ddtrace.propagation.http.HTTPPropagator.extract` — turns the envelope's
  `x-datadog-*` headers into a `Context` (Requirement 14.2).
- `tracer.get_log_correlation_context()` — the trace and span ids formatted the way
  Datadog's log correlation expects, including the 64-bit/128-bit trace-id handling
  that this worker should not be reimplementing.

Unified tagging (`DD_SERVICE`, `DD_ENV`, `DD_VERSION`) is applied by setting
`ddtrace.config` rather than by passing arguments to every span, so a span started
anywhere in the process carries the same tags as one started here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .tracing import TracerPort


class _DdtraceSpan:
    """Adapts a `ddtrace` span to `SpanPort`."""

    __slots__ = ("_span",)

    def __init__(self, span: Any) -> None:
        self._span = span

    def set_tag(self, key: str, value: Any) -> None:
        self._span.set_tag(key, value)

    def record_error(self, error: BaseException) -> None:
        # `set_exc_info` marks the span errored and attaches type, message, and
        # stack, which is what makes the span show up in Error Tracking.
        self._span.set_exc_info(type(error), error, error.__traceback__)

    def finish(self) -> None:
        self._span.finish()


class DdtracePort:
    """`ddtrace` behind the `TracerPort` protocol."""

    __slots__ = ("_extract", "_tracer")

    def __init__(self, tracer: Any, extract: Any) -> None:
        self._tracer = tracer
        self._extract = extract

    def initialize(self, *, service: str, env: str, version: str | None) -> None:
        from ddtrace import config

        config.service = service
        config.env = env
        if version is not None:
            config.version = version

    def activate(self, headers: Mapping[str, str]) -> bool:
        context = self._extract(dict(headers))
        if context is None or context.trace_id is None:
            # No usable parent in the envelope: the next span becomes a root span.
            return False
        self._tracer.context_provider.activate(context)
        return True

    def start_span(self, name: str, *, resource: str | None = None) -> _DdtraceSpan:
        return _DdtraceSpan(self._tracer.trace(name, resource=resource))

    def log_correlation(self) -> Mapping[str, str] | None:
        if self._tracer.current_span() is None:
            return None
        # `ddtrace` returns its keys already prefixed —
        # `{"dd.trace_id": ..., "dd.span_id": ..., "dd.service": ...}` — because it
        # expects them merged into a flat log line. This worker nests them under a
        # `dd` object instead, the same shape `apps/api/src/logging/logger.ts` emits,
        # so the prefix is stripped here and `service`, `env`, and `version` are
        # dropped: every log line already carries those from configuration.
        correlation = self._tracer.get_log_correlation_context()
        trace_id = correlation.get("dd.trace_id") or correlation.get("trace_id")
        span_id = correlation.get("dd.span_id") or correlation.get("span_id")
        if not trace_id or not span_id:
            return None
        return {"trace_id": str(trace_id), "span_id": str(span_id)}


def load_ddtrace_port() -> TracerPort:
    """Import `ddtrace` and wrap it. The one call that puts the library in memory."""
    from ddtrace.propagation.http import HTTPPropagator
    from ddtrace.trace import tracer

    return DdtracePort(tracer, HTTPPropagator.extract)
