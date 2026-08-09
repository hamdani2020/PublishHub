"""
Structured logging tests (Requirements 3.5, 14.3).

What matters here is the shape of the output, because a log explorer parses it:
one JSON object per line, always carrying `service` and `env`, carrying trace
identifiers when a span is active, and never breaking a single event across two
lines — not even when it includes a stack trace.

The tests write to a `StringIO` rather than stdout and supply their own trace
context, so nothing here needs a Datadog agent or an APM library installed.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from publishhub_worker.config import load_config
from publishhub_worker.logging import (
    LoggerDeps,
    TraceContextProvider,
    WorkerLogger,
    create_logger,
    format_timestamp,
)

TRACE_FIELDS = {"dd": {"trace_id": "6112637026574828042", "span_id": "1234567890123456789"}}


class Sink:
    """A logger plus a reader for what it wrote."""

    def __init__(self, logger: WorkerLogger, stream: io.StringIO) -> None:
        self.logger = logger
        self._stream = stream

    @property
    def raw(self) -> str:
        return self._stream.getvalue()

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.raw.splitlines() if line]

    def only(self) -> dict[str, Any]:
        lines = self.lines()
        assert len(lines) == 1, f"expected exactly one line, got {len(lines)}"
        return lines[0]


def build(
    env: dict[str, str] | None = None,
    *,
    level: str | None = None,
    trace_context: TraceContextProvider | None = None,
    name: str = "test-logger",
) -> Sink:
    stream = io.StringIO()
    config = load_config({} if env is None else env)
    logger = create_logger(
        config,
        LoggerDeps(stream=stream, level=level, trace_context=trace_context, name=name),
    )
    return Sink(logger, stream)


# --- line shape ---------------------------------------------------------------


def test_writes_one_json_object_per_line() -> None:
    sink = build()

    sink.logger.info("first")
    sink.logger.info("second")

    assert sink.raw.count("\n") == 2
    assert [line["msg"] for line in sink.lines()] == ["first", "second"]


def test_every_line_carries_the_service_and_environment() -> None:
    sink = build({"DD_SERVICE": "publishhub-worker", "DD_ENV": "staging"})

    sink.logger.info("job processed")
    line = sink.only()

    assert line["service"] == "publishhub-worker"
    assert line["env"] == "staging"
    assert isinstance(line["pid"], int)
    assert line["hostname"] != ""


def test_omits_version_when_the_build_does_not_stamp_one() -> None:
    sink = build()

    sink.logger.info("started")

    assert "version" not in sink.only()


def test_includes_version_when_the_build_stamps_one() -> None:
    sink = build({"DD_VERSION": "1.4.2"})

    sink.logger.info("started")

    assert sink.only()["version"] == "1.4.2"


def test_records_the_level_as_a_readable_label() -> None:
    sink = build(level="debug")

    sink.logger.debug("polling")
    sink.logger.info("received")
    sink.logger.warning("slow platform")
    sink.logger.error("publish failed")

    assert [line["level"] for line in sink.lines()] == ["debug", "info", "warn", "error"]


def test_timestamps_use_the_envelope_time_format() -> None:
    # Same format as `enqueued_at`, so timestamps compare across services.
    assert format_timestamp(1_754_560_800.123_456) == "2025-08-07T10:00:00.123Z"

    sink = build()
    sink.logger.info("started")

    assert sink.only()["time"].endswith("Z")


# --- custom fields ------------------------------------------------------------


def test_merges_custom_fields_into_the_line() -> None:
    sink = build()

    sink.logger.info(
        "job processed",
        post_id="post_01HZX3QK7M9V4TDR8N2C5EAB6F",
        attempt=2,
        duration_ms=512,
        platforms={"twitter": "published"},
    )
    line = sink.only()

    assert line["post_id"] == "post_01HZX3QK7M9V4TDR8N2C5EAB6F"
    assert line["attempt"] == 2
    assert line["duration_ms"] == 512
    assert line["platforms"] == {"twitter": "published"}


def test_bound_fields_appear_on_every_line_from_the_child() -> None:
    sink = build()

    job_logger = sink.logger.bind(job_id="3f2a9b0c", attempt=1)
    job_logger.info("job received")
    job_logger.info("job processed", duration_ms=7)
    sink.logger.info("idle")

    received, processed, idle = sink.lines()
    assert received["job_id"] == processed["job_id"] == "3f2a9b0c"
    assert processed["duration_ms"] == 7
    # Binding produces a child; the parent is unchanged.
    assert "job_id" not in idle


def test_a_per_call_field_overrides_the_bound_value() -> None:
    sink = build()

    sink.logger.bind(attempt=1).info("retrying", attempt=2)

    assert sink.only()["attempt"] == 2


def test_a_custom_field_cannot_overwrite_a_reserved_one() -> None:
    sink = build({"DD_SERVICE": "publishhub-worker"})

    sink.logger.info("job processed", service="not-the-worker", level="fatal", msg="hijacked")
    line = sink.only()

    assert line["service"] == "publishhub-worker"
    assert line["level"] == "info"
    assert line["msg"] == "job processed"


def test_an_unserializable_field_degrades_instead_of_losing_the_event() -> None:
    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    sink = build()

    sink.logger.info("job processed", handle=Opaque())

    assert sink.only()["handle"] == "<opaque>"


# --- trace correlation --------------------------------------------------------


def test_adds_trace_identifiers_when_a_span_is_active() -> None:
    sink = build(trace_context=lambda: TRACE_FIELDS)

    sink.logger.info("job processed")

    assert sink.only()["dd"] == TRACE_FIELDS["dd"]


def test_omits_trace_fields_when_no_span_is_active() -> None:
    # An initialized tracer with nothing in scope is normal, not an error.
    sink = build(trace_context=lambda: None)

    sink.logger.info("started")

    assert "dd" not in sink.only()


def test_omits_trace_fields_entirely_when_observability_is_disabled() -> None:
    # No provider is passed when tracing is off, and the line is still valid.
    sink = build({"OBSERVABILITY_ENABLED": "false"})

    sink.logger.info("started")
    line = sink.only()

    assert "dd" not in line
    assert line["service"] == "publishhub-worker"


def test_reads_the_trace_context_per_line_rather_than_once() -> None:
    spans = iter([{"dd": {"trace_id": "1"}}, {"dd": {"trace_id": "2"}}])
    sink = build(trace_context=lambda: next(spans, None))

    sink.logger.info("first")
    sink.logger.info("second")

    assert [line["dd"]["trace_id"] for line in sink.lines()] == ["1", "2"]


# --- levels and errors --------------------------------------------------------


def test_suppresses_debug_lines_outside_development() -> None:
    sink = build({"DD_ENV": "production"})

    sink.logger.debug("polling")
    sink.logger.info("received")

    assert [line["msg"] for line in sink.lines()] == ["received"]


def test_emits_debug_lines_in_development() -> None:
    sink = build({"DD_ENV": "development"})

    sink.logger.debug("polling")

    assert sink.only()["level"] == "debug"


def test_silent_turns_output_off_entirely() -> None:
    sink = build(level="silent")

    sink.logger.error("publish failed")

    assert sink.raw == ""


def test_an_exception_is_reported_inside_the_json_object() -> None:
    sink = build()

    try:
        raise TimeoutError("redis timed out")
    except TimeoutError as error:
        sink.logger.error("job failed", exc=error, attempt=3)

    line = sink.only()
    assert line["attempt"] == 3
    assert line["error"]["type"] == "TimeoutError"
    assert line["error"]["message"] == "redis timed out"
    assert "Traceback" in line["error"]["stack"]
    # The stack is escaped inside the JSON string, so one event is still one line.
    assert sink.raw.count("\n") == 1


def test_reconfiguring_does_not_duplicate_every_line() -> None:
    stream = io.StringIO()
    config = load_config({})
    deps = LoggerDeps(stream=stream, name="test-idempotent")

    create_logger(config, deps)
    logger = create_logger(config, deps)
    logger.info("started")

    assert stream.getvalue().count("\n") == 1


def test_does_not_leak_lines_to_the_root_logger(capsys: pytest.CaptureFixture[str]) -> None:
    sink = build()

    sink.logger.info("started")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
