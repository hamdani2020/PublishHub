"""
Metrics and tracing, unit by unit (Requirements 14.2, 14.4, 14.6).

Three concerns, in the order a recording travels:

1. **The DogStatsD wire format.** The worker renders its own datagrams rather than
   carrying a second Datadog package, so the exact bytes are worth asserting —
   including what happens to a tag value that would otherwise corrupt the line.
2. **Rate-limited queue-depth sampling.** Sampling costs a round trip to the broker,
   so it must not happen when metrics are inert and must not happen on every poll.
3. **The tracing seam.** `create_tracing` is exercised with the fake tracer port from
   `publishhub_worker.observability.testing`, so the real `DatadogTracing`, the real
   span lifecycle, and the real "did the envelope carry a parent trace" decision are
   all covered without `ddtrace` in the process.

Nothing here opens a socket, imports an APM library, or needs a Datadog account —
which is the point of Requirement 14.6 and is asserted directly in
`tests/test_worker_observability.py`.
"""

from __future__ import annotations

import pytest

from publishhub_worker.observability import (
    DEFAULT_DOGSTATSD_HOST,
    DEFAULT_DOGSTATSD_PORT,
    INERT_SINK,
    INERT_TRACING,
    JOB_SPAN_NAME,
    JOBS_DURATION,
    JOBS_FAILED,
    JOBS_PROCESSED,
    QUEUE_DEPTH,
    DogStatsdSink,
    QueueDepthSampler,
    Tracing,
    TracingOptions,
    UdpDatagramSender,
    WorkerMetrics,
    create_dogstatsd_sink,
    create_tracing,
    format_datagram,
    tracing_options_from_env,
)
from publishhub_worker.observability.testing import (
    SPAN_ID,
    TRACE_ID,
    FakeTracerPort,
    RecordingSink,
)

TRACE_HEADERS = {"x-datadog-trace-id": TRACE_ID, "x-datadog-parent-id": SPAN_ID}


class FakeSocket:
    """Records datagrams instead of sending them."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False
        self._fail = fail

    def sendto(self, payload: bytes, address: tuple[str, int]) -> None:
        if self._fail:
            raise OSError("no route to host")
        self.sent.append((payload, address))

    def close(self) -> None:
        self.closed = True


def enabled_metrics() -> tuple[WorkerMetrics, RecordingSink]:
    sink = RecordingSink()
    return WorkerMetrics(env="production", sink=sink), sink


def active_tracing(port: FakeTracerPort | None = None) -> tuple[Tracing, FakeTracerPort]:
    """Tracing as the bootstrap would have built it, minus the real library."""
    tracer = FakeTracerPort() if port is None else port
    options = TracingOptions(
        enabled=True,
        service="publishhub-worker",
        env="production",
        version="1.4.2",
    )
    return create_tracing(options, lambda: tracer), tracer


# --- the DogStatsD wire format ------------------------------------------------


def test_renders_a_counter_line_with_its_tags() -> None:
    line = format_datagram(JOBS_PROCESSED, 1, "c", {"platform": "twitter", "env": "prod"})

    assert line == "publishhub.jobs.processed:1|c|#platform:twitter,env:prod"


def test_renders_a_gauge_and_a_histogram_with_their_own_type_suffixes() -> None:
    assert format_datagram(QUEUE_DEPTH, 12, "g", {"backend": "redis"}) == (
        "publishhub.queue.depth:12|g|#backend:redis"
    )
    assert format_datagram(JOBS_DURATION, 240, "h", {}) == "publishhub.jobs.duration:240|h"


def test_writes_whole_numbers_without_a_decimal_point() -> None:
    # A float gauge reading 12.0 is still a depth of twelve.
    assert format_datagram(QUEUE_DEPTH, 12.0, "g", {}) == "publishhub.queue.depth:12|g"
    assert format_datagram(JOBS_DURATION, 12.5, "h", {}) == "publishhub.jobs.duration:12.5|h"


def test_neutralizes_tag_values_that_would_corrupt_the_datagram() -> None:
    # `DD_ENV` is operator-supplied, so a comma would silently split one tag into
    # two and a newline would forge a second metric.
    line = format_datagram(JOBS_FAILED, 1, "c", {"env": "prod,staging\npublishhub.evil:1|c"})

    assert line == "publishhub.jobs.failed:1|c|#env:prod_staging_publishhub.evil:1_c"
    assert "\n" not in line


def test_sends_each_recording_as_one_encoded_datagram() -> None:
    payloads: list[bytes] = []
    sink = DogStatsdSink(payloads.append)

    sink.increment(JOBS_PROCESSED, 1, {"env": "prod"})
    sink.gauge(QUEUE_DEPTH, 3, {"env": "prod"})

    assert payloads == [
        b"publishhub.jobs.processed:1|c|#env:prod",
        b"publishhub.queue.depth:3|g|#env:prod",
    ]


def test_the_udp_sender_addresses_the_local_agent_and_reuses_its_socket() -> None:
    fake = FakeSocket()
    sender = UdpDatagramSender(host="10.0.0.1", port=8125, create_socket=lambda: fake)

    sender(b"publishhub.jobs.processed:1|c")
    sender(b"publishhub.jobs.failed:1|c")

    assert [address for _payload, address in fake.sent] == [("10.0.0.1", 8125)] * 2
    assert sender.failed_sends == 0


def test_a_send_failure_is_counted_and_never_raised() -> None:
    # An Agent that is not listening must not turn a metric into a failed job.
    fake = FakeSocket(fail=True)
    sender = UdpDatagramSender(create_socket=lambda: fake)

    sender(b"publishhub.jobs.processed:1|c")

    assert sender.failed_sends == 1
    assert sender.address == (DEFAULT_DOGSTATSD_HOST, DEFAULT_DOGSTATSD_PORT)


def test_reads_the_agent_address_from_the_environment() -> None:
    sink = create_dogstatsd_sink(
        {"DD_AGENT_HOST": "datadog.publishhub.svc", "DD_DOGSTATSD_PORT": "8135"},
        create_socket=lambda: FakeSocket(),
    )

    assert sink.sender.address == ("datadog.publishhub.svc", 8135)


def test_falls_back_to_the_default_port_rather_than_refusing_to_start() -> None:
    # The transport address is not worth failing a worker over; the configuration
    # loader is the place that refuses to boot on a bad value.
    sink = create_dogstatsd_sink({"DD_DOGSTATSD_PORT": "eight-thousand"})

    assert sink.sender.address == (DEFAULT_DOGSTATSD_HOST, DEFAULT_DOGSTATSD_PORT)


# --- what the worker records ---------------------------------------------------


def test_tags_every_job_metric_with_platform_status_and_environment() -> None:
    metrics, sink = enabled_metrics()

    metrics.job_processed(platform="twitter", status="published")
    metrics.job_failed(platform="linkedin", status="retrying")
    metrics.job_duration(platform="twitter", status="published", duration_ms=240)

    assert sink.tags(JOBS_PROCESSED) == [
        {"platform": "twitter", "status": "published", "env": "production"}
    ]
    assert sink.tags(JOBS_FAILED) == [
        {"platform": "linkedin", "status": "retrying", "env": "production"}
    ]
    assert sink.named(JOBS_DURATION) == [
        (JOBS_DURATION, 240, {"platform": "twitter", "status": "published", "env": "production"})
    ]


def test_tags_queue_depth_with_the_active_backend() -> None:
    metrics, sink = enabled_metrics()

    metrics.queue_depth_observed(backend="sqs", depth=42)

    assert sink.named(QUEUE_DEPTH) == [(QUEUE_DEPTH, 42, {"backend": "sqs", "env": "production"})]


def test_metrics_are_inert_by_default_so_local_runs_export_nothing() -> None:
    metrics = WorkerMetrics(env="development")

    metrics.job_processed(platform="twitter", status="published")

    assert metrics.enabled is False
    assert metrics.sink is INERT_SINK


# --- rate-limited queue-depth sampling ----------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_never_asks_the_broker_for_a_depth_nobody_will_record() -> None:
    # Requirement 14.6: disabled means no work, not discarded work.
    sampler = QueueDepthSampler(WorkerMetrics(env="development"), backend="redis")

    def unreachable() -> int:
        raise AssertionError("depth must not be sampled when metrics are inert")

    assert sampler.sample(unreachable) is None
    assert sampler.due() is False


def test_samples_once_and_then_waits_out_the_interval() -> None:
    metrics, sink = enabled_metrics()
    clock = Clock()
    sampler = QueueDepthSampler(
        metrics, backend="redis", interval_seconds=15.0, monotonic=clock
    )
    calls: list[int] = []

    def depth() -> int:
        calls.append(1)
        return len(calls)

    assert sampler.sample(depth) == 1
    clock.advance(14.0)
    assert sampler.sample(depth) is None
    clock.advance(1.0)
    assert sampler.sample(depth) == 2

    assert sink.values(QUEUE_DEPTH) == [1, 2]


def test_leaves_a_failing_depth_call_to_the_caller() -> None:
    # The job loop owns the logger, so it owns the decision to shrug this off.
    metrics, _sink = enabled_metrics()
    sampler = QueueDepthSampler(metrics, backend="redis")

    def broken() -> int:
        raise ConnectionError("redis went away")

    with pytest.raises(ConnectionError):
        sampler.sample(broken)


# --- the tracing switch (Requirement 14.6) ------------------------------------


def test_never_loads_the_tracer_when_the_switch_is_off() -> None:
    def unreachable():
        raise AssertionError("the tracer must not be loaded when observability is off")

    tracing = create_tracing(
        TracingOptions(enabled=False, service="publishhub-worker", env="development", version=None),
        unreachable,
    )

    assert tracing is INERT_TRACING
    assert tracing.enabled is False


def test_the_disabled_path_still_hands_the_loop_a_usable_span() -> None:
    with INERT_TRACING.job_span(trace_context=TRACE_HEADERS, tags={"post.id": "post_x"}) as span:
        span.set_tags(**{"job.status": "published"})

    assert INERT_TRACING.trace_context() is None


def test_reads_the_switch_and_the_unified_tags_from_the_environment() -> None:
    options = tracing_options_from_env(
        {
            "OBSERVABILITY_ENABLED": "true",
            "DD_SERVICE": "publishhub-worker",
            "DD_ENV": "staging",
            "DD_VERSION": "  1.4.2  ",
        }
    )

    assert options.enabled is True
    assert (options.service, options.env, options.version) == (
        "publishhub-worker",
        "staging",
        "1.4.2",
    )


def test_an_unrecognized_switch_value_counts_as_off_rather_than_fatal() -> None:
    # There is no logger this early; `load_config` refuses to boot and names the key.
    assert tracing_options_from_env({"OBSERVABILITY_ENABLED": "yes-please"}).enabled is False
    assert tracing_options_from_env({}).enabled is False


def test_a_tracer_that_fails_to_load_leaves_the_worker_running_untraced() -> None:
    reported: list[BaseException] = []

    def broken():
        raise ImportError("No module named 'ddtrace'")

    tracing = create_tracing(
        TracingOptions(
            enabled=True,
            service="publishhub-worker",
            env="production",
            version=None,
            on_load_error=reported.append,
        ),
        broken,
    )

    assert tracing is INERT_TRACING
    assert [type(error).__name__ for error in reported] == ["ImportError"]


# --- the enabled path (Requirement 14.2) --------------------------------------


def test_initializes_the_tracer_with_the_unified_tags() -> None:
    _tracing, tracer = active_tracing()

    assert tracer.initialized == {
        "service": "publishhub-worker",
        "env": "production",
        "version": "1.4.2",
    }


def test_continues_the_trace_the_envelope_carried() -> None:
    tracing, tracer = active_tracing()

    with tracing.job_span(
        trace_context=TRACE_HEADERS,
        resource="twitter,linkedin",
        tags={"post.id": "post_x", "job.attempt": 1},
    ):
        pass

    assert tracer.activated == [TRACE_HEADERS]
    span = tracer.span
    assert span.name == JOB_SPAN_NAME
    assert span.resource == "twitter,linkedin"
    assert span.parent_trace_id == TRACE_ID
    assert span.tags == {"post.id": "post_x", "job.attempt": 1}
    assert span.finished is True


def test_starts_a_root_span_when_the_producer_had_tracing_off() -> None:
    # `{}` is the documented contract, not a degraded state.
    tracing, tracer = active_tracing()

    with tracing.job_span(trace_context={}):
        pass

    assert tracer.activated == []
    assert tracer.span.parent_trace_id is None


def test_tags_applied_after_the_outcome_land_on_the_same_span() -> None:
    tracing, tracer = active_tracing()

    with tracing.job_span(trace_context={}, tags={"post.id": "post_x"}) as span:
        span.set_tags(**{"job.status": "retrying", "job.next_attempt": 2})

    assert tracer.span.tags == {
        "post.id": "post_x",
        "job.status": "retrying",
        "job.next_attempt": 2,
    }


def test_drops_tags_with_no_value_instead_of_writing_the_string_none() -> None:
    tracing, tracer = active_tracing()

    with tracing.job_span(trace_context={}, tags={"post.id": None, "job.attempt": 1}):
        pass

    assert tracer.span.tags == {"job.attempt": 1}


def test_offers_the_active_span_ids_for_log_correlation() -> None:
    tracing, _tracer = active_tracing()

    with tracing.job_span(trace_context=TRACE_HEADERS):
        assert tracing.trace_context() == {"dd": {"trace_id": TRACE_ID, "span_id": SPAN_ID}}

    # Outside the span there is nothing to correlate, so the fields are absent
    # rather than present and empty.
    assert tracing.trace_context() is None


def test_marks_the_span_errored_when_the_job_raises_and_still_finishes_it() -> None:
    # `job_loop` lets an infrastructure failure escape on purpose; the span has to
    # record it and close either way.
    tracing, tracer = active_tracing()
    failure = ConnectionError("redis went away")

    with pytest.raises(ConnectionError):
        with tracing.job_span(trace_context={}):
            raise failure

    assert tracer.span.errors == [failure]
    assert tracer.span.finished is True


@pytest.mark.parametrize("method", ["activate", "start_span"])
def test_a_misbehaving_tracer_does_not_break_the_job(method: str) -> None:
    # Tracing is optional; publishing is not.
    tracing, tracer = active_tracing(FakeTracerPort(fail_on=frozenset({method})))

    with tracing.job_span(trace_context=TRACE_HEADERS, tags={"post.id": "post_x"}) as span:
        span.set_tags(**{"job.status": "published"})

    assert tracer.spans == [] or tracer.spans[0].tags == {}


def test_a_tracer_that_cannot_report_ids_does_not_break_a_log_line() -> None:
    tracing, _tracer = active_tracing(FakeTracerPort(fail_on=frozenset({"log_correlation"})))

    with tracing.job_span(trace_context={}):
        assert tracing.trace_context() is None
