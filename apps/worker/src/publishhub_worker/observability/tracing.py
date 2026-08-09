"""
Datadog tracing, behind a seam (Requirements 14.2, 14.3, 14.6).

Nothing in this file imports `ddtrace`. The tracer arrives as a loader function,
so the decision "should this process trace at all" is made once, at startup, and
the library is only pulled into the process when the answer is yes. That is what
makes `OBSERVABILITY_ENABLED=false` genuinely inert rather than merely quiet: an
APM library that has been imported has already patched `redis` and `botocore`,
whatever its configuration says afterwards. It is also why every test here runs
with no Datadog agent and no `ddtrace` installed.

What the rest of the worker gets is `Tracing`: two questions with answers
that are safe to use whether or not tracing is on.

| Member           | Tracing on                                    | Tracing off  |
|------------------|-----------------------------------------------|--------------|
| `trace_context()`| `{"dd": {"trace_id": ..., "span_id": ...}}`    | `None`       |
| `job_span(...)`  | a child of the API's span, tagged and finished | a no-op span |

`job_span` is the worker half of Requirement 14.2. The API injects its span's
propagation headers into the message envelope's `trace_context`
(`docs/message-schema.md`); this module extracts them and starts a span that
continues that trace, so one publish is one trace across two services and two
languages. When the envelope carries `{}` — which is what the API sends with
tracing off — the worker starts a root span instead, which is the documented
behavior rather than a degraded one.

## Tracing never breaks a job

Every call into the tracer is wrapped. A loader that raises, an `extract` that
chokes on a malformed header, a span that cannot be started: each of them degrades
to "no span" and lets the job run. A worker that dropped posts because an APM
library had a bad day would be a worse outcome than a worker with no traces, and
the whole argument for `OBSERVABILITY_ENABLED` is that observability is optional.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

# Imported from `config.flags` rather than from `config`, and that is deliberate:
# `config/__init__.py` pulls in the queue package, and this module is evaluated by
# the bootstrap that has to run before anything a tracer might want to patch.
# `config/flags.py` imports nothing at all.
from ..config.flags import parse_boolean_flag

#: An environment mapping, spelled here rather than imported from the queue package
#: for the same reason.
Env = Mapping[str, str]

#: Operation name of the worker's span. `resource` carries the platform list, so
#: the operation stays one aggregatable name in the Datadog UI.
JOB_SPAN_NAME = "publishhub.worker.job"

#: Defaults for the unified-tagging variables, matching `CONFIG_DEFAULTS`.
DEFAULT_SERVICE = "publishhub-worker"
DEFAULT_ENV = "development"


class SpanPort(Protocol):
    """
    The span operations the worker uses. Structural, so the `ddtrace` adapter and
    the test fake are interchangeable and neither is special.
    """

    def set_tag(self, key: str, value: Any) -> None: ...

    def record_error(self, error: BaseException) -> None: ...

    def finish(self) -> None: ...


class TracerPort(Protocol):
    """
    The tracer operations the worker uses, in the order the job loop needs them.

    Implemented for real in `ddtrace_loader.py` — the only module in the worker
    that names `ddtrace` — and faked in `observability/testing/fake_tracer.py`.
    """

    def initialize(self, *, service: str, env: str, version: str | None) -> None:
        """Configure the tracer. Called once, immediately after loading."""

    def activate(self, headers: Mapping[str, str]) -> bool:
        """
        Make the trace described by `headers` the active parent, and report whether
        one was found. `False` means the next span is a root span.
        """

    def start_span(self, name: str, *, resource: str | None = None) -> SpanPort: ...

    def log_correlation(self) -> Mapping[str, str] | None:
        """
        `{"trace_id": ..., "span_id": ...}` for the active span, or `None` when
        there is none — a startup line, a shutdown line, an idle poll.
        """


#: Loads and returns the port. Called at most once, and only when enabled.
TracerLoader = Callable[[], TracerPort]


class JobSpan(Protocol):
    """
    What the job loop holds. Always present, so the loop never branches on whether
    tracing is on: with it off, these calls do nothing.

    `set_tags` is the only member a caller needs; `record_error` and `finish` belong
    to the context manager that yielded the span and are not called from the loop.
    """

    def set_tags(self, **tags: Any) -> None: ...

    def record_error(self, error: BaseException) -> None: ...

    def finish(self) -> None: ...


class _InertSpan:
    """The disabled span. Accepts every call and records none."""

    __slots__ = ()

    def set_tags(self, **tags: Any) -> None:
        return None

    def record_error(self, error: BaseException) -> None:
        return None

    def finish(self) -> None:
        return None


#: Shared no-op span, yielded whenever there is nothing to record.
NO_SPAN: JobSpan = _InertSpan()


class _RecordingSpan:
    """Adapts a `SpanPort` to `JobSpan`, swallowing tracer failures."""

    __slots__ = ("_span",)

    def __init__(self, span: SpanPort) -> None:
        self._span = span

    def set_tags(self, **tags: Any) -> None:
        for key, value in tags.items():
            if value is None:
                # An absent tag rather than the string "None", which is what a
                # Datadog facet would otherwise fill up with.
                continue
            try:
                self._span.set_tag(key, value)
            # Deliberately broad: a tag is not worth an exception on the job path.
            except Exception:
                return None

    def record_error(self, error: BaseException) -> None:
        try:
            self._span.record_error(error)
        except Exception:
            return None

    def finish(self) -> None:
        try:
            self._span.finish()
        except Exception:
            return None


class Tracing(Protocol):
    """The seam the worker programs against."""

    @property
    def enabled(self) -> bool:
        """True only when a tracer was actually loaded and initialized."""

    def trace_context(self) -> Mapping[str, Any] | None:
        """Log fields identifying the active span, or `None` when there is none."""

    def job_span(
        self,
        *,
        trace_context: Mapping[str, str],
        resource: str | None = None,
        tags: Mapping[str, Any] | None = None,
    ) -> Any:
        """
        Context manager yielding a `JobSpan` for one delivery attempt,
        continuing the trace in `trace_context` when it describes one.
        """


class InertTracing:
    """The disabled path: no tracer, no span, no fields."""

    __slots__ = ()

    @property
    def enabled(self) -> bool:
        return False

    def trace_context(self) -> Mapping[str, Any] | None:
        return None

    @contextmanager
    def job_span(
        self,
        *,
        trace_context: Mapping[str, str],
        resource: str | None = None,
        tags: Mapping[str, Any] | None = None,
    ) -> Iterator[JobSpan]:
        yield NO_SPAN


#: Shared disabled instance.
INERT_TRACING: Tracing = InertTracing()


@dataclass(frozen=True, kw_only=True, slots=True)
class TracingOptions:
    """The four things the tracer needs, plus a way to report a failed load."""

    enabled: bool
    service: str
    env: str
    version: str | None
    #: Called when loading or initializing the tracer raises. Injected so a test
    #: need not touch stderr, and so the bootstrap can write there before a logger
    #: exists.
    on_load_error: Callable[[BaseException], None] | None = None


def tracing_options_from_env(env: Env) -> TracingOptions:
    """
    Read the switch and the unified tags straight from the environment.

    `load_config` is deliberately not used: this runs before it can be imported,
    because importing it pulls in the queue clients — the very modules `ddtrace`
    has to patch before they are evaluated. An unrecognized `OBSERVABILITY_ENABLED`
    counts as off rather than fatal; there is no logger this early, and
    `load_config` is milliseconds away and will refuse to start with the offending
    key named (Requirement 5.5).
    """
    return TracingOptions(
        enabled=parse_boolean_flag(env.get("OBSERVABILITY_ENABLED")) is True,
        service=_blank_to_none(env.get("DD_SERVICE")) or DEFAULT_SERVICE,
        env=_blank_to_none(env.get("DD_ENV")) or DEFAULT_ENV,
        version=_blank_to_none(env.get("DD_VERSION")),
    )


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return None if trimmed == "" else trimmed


class DatadogTracing:
    """Tracing through a loaded, initialized `TracerPort`."""

    __slots__ = ("_port",)

    def __init__(self, port: TracerPort) -> None:
        self._port = port

    @property
    def enabled(self) -> bool:
        return True

    def trace_context(self) -> Mapping[str, Any] | None:
        """
        `{"dd": {"trace_id": ..., "span_id": ...}}` — the shape Datadog's log
        correlation expects, and the same shape `apps/api/src/logging/logger.ts`
        emits, so one query finds both services' lines.
        """
        try:
            correlation = self._port.log_correlation()
        # Broad on purpose: this runs inside the log formatter, where raising would
        # lose the log line itself.
        except Exception:
            return None
        if not correlation:
            return None
        return {"dd": dict(correlation)}

    @contextmanager
    def job_span(
        self,
        *,
        trace_context: Mapping[str, str],
        resource: str | None = None,
        tags: Mapping[str, Any] | None = None,
    ) -> Iterator[JobSpan]:
        span = self._start(trace_context, resource)
        if tags:
            span.set_tags(**tags)
        try:
            yield span
        except BaseException as error:
            # An exception escaping the job is exactly what an errored span is for;
            # `job_loop` lets infrastructure failures propagate by design.
            span.record_error(error)
            raise
        finally:
            span.finish()

    def _start(self, trace_context: Mapping[str, str], resource: str | None) -> JobSpan:
        """A child span, or `NO_SPAN` if the tracer would not cooperate."""
        try:
            if trace_context:
                # Empty means the producer had tracing off, so there is no parent to
                # continue and this becomes a root span — the documented contract.
                self._port.activate(trace_context)
            return _RecordingSpan(self._port.start_span(JOB_SPAN_NAME, resource=resource))
        except Exception:
            return NO_SPAN


def create_tracing(options: TracingOptions, load: TracerLoader) -> Tracing:
    """
    Initialize tracing, or don't.

    Returns `INERT_TRACING` when the switch is off — without calling the
    loader at all — and also when the loader or `initialize` raises: a missing or
    broken APM library must not stop the worker from processing jobs.
    """
    if not options.enabled:
        return INERT_TRACING

    try:
        port = load()
        port.initialize(service=options.service, env=options.env, version=options.version)
    except Exception as error:
        if options.on_load_error is not None:
            options.on_load_error(error)
        return INERT_TRACING

    return DatadogTracing(port)
