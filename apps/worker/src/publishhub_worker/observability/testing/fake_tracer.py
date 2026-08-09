"""
A tracer and a metrics sink that record instead of exporting.

Shipped inside the package rather than under `tests/` so the integration suite in
spec task 6.2 reuses these instead of writing its own, and so no test in this
repository ever needs `ddtrace` installed or a Datadog Agent listening.

`FakeTracerPort` implements the same `TracerPort` protocol the `ddtrace`
adapter does, which is what makes the tracing tests meaningful: they exercise the
real `DatadogTracing`, the real context-manager lifecycle, and the real decision
about whether the envelope carried a parent trace.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: Ids a fake span reports, in the same shape Datadog's log correlation uses.
TRACE_ID = "6249442685991245312"
SPAN_ID = "8114249130118331704"


@dataclass(slots=True)
class FakeSpan:
    """One started span, with everything that happened to it."""

    name: str
    resource: str | None = None
    #: The trace this span continues, or `None` when it is a root span.
    parent_trace_id: str | None = None
    tags: dict[str, Any] = field(default_factory=dict)
    errors: list[BaseException] = field(default_factory=list)
    finished: bool = False

    def set_tag(self, key: str, value: Any) -> None:
        self.tags[key] = value

    def record_error(self, error: BaseException) -> None:
        self.errors.append(error)

    def finish(self) -> None:
        self.finished = True


@dataclass(slots=True)
class FakeTracerPort:
    """
    Records initialization, extraction, and every span started.

    `fail_on` names a method that should raise instead of working, so a test can
    prove that a misbehaving tracer degrades to "no span" rather than failing the
    job — which is the whole reason `DatadogTracing` wraps every call.
    """

    #: `x-datadog-trace-id` header name, the field `activate` reads to decide whether
    #: the envelope described a parent.
    trace_id_header: str = "x-datadog-trace-id"
    initialized: dict[str, str | None] | None = None
    activated: list[Mapping[str, str]] = field(default_factory=list)
    spans: list[FakeSpan] = field(default_factory=list)
    fail_on: frozenset[str] = frozenset()
    _active_trace_id: str | None = None

    @property
    def span(self) -> FakeSpan:
        """The only span started. Asserts that there was exactly one."""
        assert len(self.spans) == 1, f"expected one span, got {len(self.spans)}"
        return self.spans[0]

    def initialize(self, *, service: str, env: str, version: str | None) -> None:
        self._maybe_fail("initialize")
        self.initialized = {"service": service, "env": env, "version": version}

    def activate(self, headers: Mapping[str, str]) -> bool:
        self._maybe_fail("activate")
        self.activated.append(dict(headers))
        self._active_trace_id = headers.get(self.trace_id_header)
        return self._active_trace_id is not None

    def start_span(self, name: str, *, resource: str | None = None) -> FakeSpan:
        self._maybe_fail("start_span")
        span = FakeSpan(name=name, resource=resource, parent_trace_id=self._active_trace_id)
        self.spans.append(span)
        return span

    def log_correlation(self) -> Mapping[str, str] | None:
        self._maybe_fail("log_correlation")
        if not self.spans or self.spans[-1].finished:
            # Matches a real tracer: no span in scope means no fields, not an error.
            return None
        return {"trace_id": TRACE_ID, "span_id": SPAN_ID}

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail_on:
            raise RuntimeError(f"fake tracer was asked to fail in {method}")


@dataclass(slots=True)
class RecordingSink:
    """
    A `MetricsSink` that keeps what it was given.

    Recordings are kept as `(name, value, tags)` triples in call order, so a test
    asserts on the metric name, the value, *and* the tag set — the three things a
    dashboard or a monitor actually queries.
    """

    recordings: list[tuple[str, float, dict[str, str]]] = field(default_factory=list)

    def increment(self, name: str, value: int, tags: Mapping[str, str]) -> None:
        self.recordings.append((name, value, dict(tags)))

    def gauge(self, name: str, value: float, tags: Mapping[str, str]) -> None:
        self.recordings.append((name, value, dict(tags)))

    def histogram(self, name: str, value: float, tags: Mapping[str, str]) -> None:
        self.recordings.append((name, value, dict(tags)))

    def named(self, name: str) -> list[tuple[str, float, dict[str, str]]]:
        """Every recording of one metric, in order."""
        return [recording for recording in self.recordings if recording[0] == name]

    def values(self, name: str) -> list[float]:
        return [value for _name, value, _tags in self.named(name)]

    def tags(self, name: str) -> list[dict[str, str]]:
        return [tags for _name, _value, tags in self.named(name)]
