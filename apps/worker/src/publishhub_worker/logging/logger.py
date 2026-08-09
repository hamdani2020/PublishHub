"""
Structured logging (Requirements 3.5, 14.3).

One JSON object per line, on stdout, carrying `service`, `env`, and — when a
tracer is active — the trace identifiers, so a log line and the span that
produced it correlate in Datadog without any manual stitching.

Nothing here imports `ddtrace`. The trace fields arrive through a
`trace_context` callable supplied by the observability wiring (spec task 4.5);
with tracing off, no provider is passed and the fields simply never appear. That
is what lets the worker run, and these tests pass, with no Datadog account and no
APM library installed (Requirement 14.6).

Two deliberate choices about the output shape, both matching
`apps/api/src/logging/logger.ts` so the two services look the same in a log
explorer:

- `level` is a lowercase string, not stdlib's `WARNING` or pino's numeric
  default. A number is cheaper to write and useless to read.
- Timestamps are RFC 3339 UTC with millisecond precision, the same format the
  message envelope uses for `enqueued_at`.

The stdlib `logging` module does the level filtering and the writing; this module
supplies the formatter and a thin call-site wrapper. Using stdlib logging rather
than a JSON logging package keeps the runtime dependency list at what the queue
clients need, and means a library that logs through `logging` (redis-py, botocore)
can be routed to the same handler.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO

from ..config import WorkerConfig

#: Returns the fields that tie a log line to the active span — for Datadog,
#: `{"dd": {"trace_id": ..., "span_id": ...}}` — or `None` when no span is
#: active, so the keys are absent from the line rather than present and null.
TraceContextProvider = Callable[[], Mapping[str, Any] | None]

#: Custom fields ride on the record under one attribute, so they can never
#: collide with a `LogRecord` attribute the way a flat `extra=` dict can.
FIELDS_ATTRIBUTE = "publishhub_fields"

#: Keys owned by the formatter. A custom field using one of these names is
#: dropped rather than allowed to overwrite it: a log line whose `service` came
#: from a caller's typo is worse than a missing field.
RESERVED_FIELDS: frozenset[str] = frozenset(
    {"time", "level", "msg", "service", "env", "version", "pid", "hostname"}
)

#: stdlib level names to pino's labels, so `warn` means `warn` in both services.
LEVEL_LABELS: Mapping[int, str] = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "fatal",
}

#: Level names accepted by `LoggerDeps.level`. `silent` sits above `CRITICAL`, so
#: a test or a one-off script can turn output off entirely.
SILENT_LEVEL = logging.CRITICAL + 10
LEVEL_NUMBERS: Mapping[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
    "silent": SILENT_LEVEL,
}


def format_timestamp(epoch_seconds: float) -> str:
    """RFC 3339 UTC with millisecond precision, e.g. `2026-08-07T10:00:00.000Z`."""
    moment = datetime.fromtimestamp(epoch_seconds, tz=UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


class JsonFormatter(logging.Formatter):
    """
    Renders a `LogRecord` as one JSON object.

    `base` is merged into every line (service, env, version, pid, hostname).
    `trace_context` is called per line, because the active span changes from one
    line to the next.
    """

    def __init__(
        self,
        *,
        base: Mapping[str, Any],
        trace_context: TraceContextProvider | None = None,
    ) -> None:
        super().__init__()
        self._base = dict(base)
        self._trace_context = trace_context

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": format_timestamp(record.created),
            "level": LEVEL_LABELS.get(record.levelno, record.levelname.lower()),
            "msg": record.getMessage(),
            **self._base,
        }

        if self._trace_context is not None:
            # A tracer that is initialized but has no span in scope is normal —
            # startup and shutdown lines, an idle poll. Not an error condition, so
            # it produces no fields rather than a warning.
            payload.update(self._trace_context() or {})

        fields = getattr(record, FIELDS_ATTRIBUTE, None)
        if isinstance(fields, Mapping):
            payload.update(
                {key: value for key, value in fields.items() if key not in RESERVED_FIELDS}
            )

        if record.exc_info is not None:
            exception = record.exc_info[1]
            payload["error"] = {
                "type": type(exception).__name__ if exception is not None else "unknown",
                "message": str(exception) if exception is not None else "",
                # The stack goes in the JSON object rather than trailing the line
                # as stdlib does by default, so one event stays one parseable line.
                "stack": self.formatException(record.exc_info),
            }

        # `default=str` so an unserializable field degrades to its `repr` instead
        # of raising inside logging and losing the event.
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


class WorkerLogger:
    """
    Call-site wrapper: `logger.info("job processed", post_id=..., attempt=...)`.

    `bind` returns a child carrying extra fields on every line, which is how the
    job loop attaches `job_id`, `post_id`, and `attempt` once instead of at every
    call (Requirement 3.5).
    """

    __slots__ = ("_fields", "_logger")

    def __init__(self, logger: logging.Logger, fields: Mapping[str, Any] | None = None) -> None:
        self._logger = logger
        self._fields: dict[str, Any] = {} if fields is None else dict(fields)

    @property
    def fields(self) -> Mapping[str, Any]:
        """The bound fields, for inspection in tests."""
        return dict(self._fields)

    def bind(self, **fields: Any) -> WorkerLogger:
        """A logger with these fields added to every line it writes."""
        return WorkerLogger(self._logger, {**self._fields, **fields})

    def debug(self, event: str, **fields: Any) -> None:
        self._log(logging.DEBUG, event, None, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._log(logging.INFO, event, None, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._log(logging.WARNING, event, None, fields)

    def error(self, event: str, *, exc: BaseException | None = None, **fields: Any) -> None:
        self._log(logging.ERROR, event, exc, fields)

    def _log(
        self,
        level: int,
        event: str,
        exc: BaseException | None,
        fields: Mapping[str, Any],
    ) -> None:
        self._logger.log(
            level,
            event,
            exc_info=exc,
            extra={FIELDS_ATTRIBUTE: {**self._fields, **fields}},
        )


@dataclass(frozen=True, slots=True)
class LoggerDeps:
    """Construction seams, so a test reads what was written instead of stdout."""

    #: Where lines go. Defaults to `sys.stdout`; the container runtime collects it.
    stream: TextIO | None = None
    #: Overrides the level derived from the environment name.
    level: str | None = None
    trace_context: TraceContextProvider | None = None
    #: Overrides the logger name, which defaults to `DD_SERVICE`.
    name: str | None = None


def create_logger(config: WorkerConfig, deps: LoggerDeps | None = None) -> WorkerLogger:
    """
    Build the worker's logger from validated configuration.

    Calling this twice with the same configuration replaces the handler rather
    than adding a second one, so a re-configured process does not emit every line
    twice.
    """
    dependencies = LoggerDeps() if deps is None else deps
    observability = config.observability

    base: dict[str, Any] = {
        "service": observability.service,
        "env": observability.env,
    }
    if observability.version is not None:
        base["version"] = observability.version
    base["pid"] = os.getpid()
    base["hostname"] = socket.gethostname()

    handler = logging.StreamHandler(
        sys.stdout if dependencies.stream is None else dependencies.stream
    )
    handler.setFormatter(JsonFormatter(base=base, trace_context=dependencies.trace_context))

    logger = logging.getLogger(dependencies.name or observability.service)
    # Replace rather than append: `create_logger` is idempotent.
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
    logger.addHandler(handler)
    logger.setLevel(resolve_level(dependencies.level or config.log_level))
    # The root logger's default handler writes unstructured text to stderr;
    # propagating would print every line twice, once in each format.
    logger.propagate = False

    return WorkerLogger(logger)


def resolve_level(level: str) -> int:
    """
    Level number for a name. Unrecognized names fall back to `info` rather than
    raising: the level is derived from configuration that has already been
    validated, and a logger is not worth crashing a process over.
    """
    return LEVEL_NUMBERS.get(level.strip().lower(), logging.INFO)
