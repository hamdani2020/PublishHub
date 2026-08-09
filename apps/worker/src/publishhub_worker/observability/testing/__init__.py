"""
Test doubles for the observability module: a tracer that records spans and a metrics
sink that keeps recordings.

Both live inside the package for the same reason the queue's fakes do — so later
suites reuse them rather than reinventing them — and both exist so that no test in
this repository needs `ddtrace`, a UDP socket, or a Datadog account.
"""

from .fake_tracer import (
    SPAN_ID,
    TRACE_ID,
    FakeSpan,
    FakeTracerPort,
    RecordingSink,
)

__all__ = [
    "SPAN_ID",
    "TRACE_ID",
    "FakeSpan",
    "FakeTracerPort",
    "RecordingSink",
]
