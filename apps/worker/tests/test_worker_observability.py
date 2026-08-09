"""
Observability where it meets the job loop (Requirements 3.5, 14.2, 14.4, 14.6).

The unit behavior of the metrics sink and the tracing seam lives in
`tests/test_observability.py`. What is asserted here is the wiring: that a real
`JobLoop`, driving the real `RedisQueueClient` and `RedisPostStore` against the
in-memory fake, emits the design's four metrics with the right tags for every
outcome the loop can produce, starts a span that continues the trace the envelope
carried, logs each outcome with the fields Requirement 3.5 names, and — the other
half of the requirement — behaves *identically* with observability switched off.

That last one is the requirement worth reading twice. "Inert" here does not mean
"records into a bin": with the switch off the worker starts no span, sends no
datagram, and never asks the broker how deep the queue is, so a local run costs
exactly what it cost before this task.
"""

from __future__ import annotations

import io
import json
from typing import Any

from publishhub_worker.config import WorkerConfig, load_config
from publishhub_worker.logging import LoggerDeps, create_logger
from publishhub_worker.observability import (
    INERT_OBSERVABILITY,
    INERT_TRACING,
    JOB_SPAN_NAME,
    JOBS_DURATION,
    JOBS_FAILED,
    JOBS_PROCESSED,
    NO_PLATFORM,
    QUEUE_DEPTH,
    TracingOptions,
    WorkerMetrics,
    WorkerObservability,
    bootstrap,
    create_observability,
    create_tracing,
)
from publishhub_worker.observability.testing import (
    SPAN_ID,
    TRACE_ID,
    FakeTracerPort,
    RecordingSink,
)
from publishhub_worker.posts import PlatformResult, RedisPostStore
from publishhub_worker.processing import JobLoop, JobLoopDeps, JobOutcome, job_disposition
from publishhub_worker.queue import (
    DEFAULT_REDIS_QUEUE_KEYS,
    PublishJob,
    RedisQueueClient,
    create_publish_job,
)
from publishhub_worker.queue.testing import FakeRedis
from publishhub_worker.runtime import EXIT_OK, RuntimeDeps, run_worker

KEYS = DEFAULT_REDIS_QUEUE_KEYS
POST_ID = "post_01HZX3QK7M9V4TDR8N2C5EAB6F"
RECORD_KEY = f"publishhub:post:{POST_ID}"
TRACE_HEADERS = {"x-datadog-trace-id": TRACE_ID, "x-datadog-parent-id": SPAN_ID}


class Clock:
    """Monotonic time under the test's control, for the depth sampler's interval."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Harness:
    """A wired loop, the fakes behind it, and whatever it recorded."""

    def __init__(
        self,
        *,
        loop: JobLoop,
        queue: RedisQueueClient,
        redis: FakeRedis,
        sink: RecordingSink,
        tracer: FakeTracerPort | None,
        stream: io.StringIO,
        clock: Clock,
    ) -> None:
        self.loop = loop
        self.queue = queue
        self.redis = redis
        self.sink = sink
        self.tracer = tracer
        self.clock = clock
        self._stream = stream

    def record(self) -> dict[str, str]:
        return self.redis.fields(RECORD_KEY)

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self._stream.getvalue().splitlines() if line]

    def find(self, event: str) -> dict[str, Any]:
        matches = [line for line in self.lines() if line["msg"] == event]
        assert len(matches) == 1, f'expected one "{event}" line, got {len(matches)}'
        return matches[0]

    def commands(self) -> list[str]:
        return [call[0] for call in self.redis.calls]


def make_config(**overrides: str) -> WorkerConfig:
    return load_config(
        {
            "POLL_WAIT_SECONDS": "7",
            "SIMULATE_LATENCY_MS": "0",
            "SIMULATE_FAILURE_RATE": "0",
            "DD_ENV": "production",
            **overrides,
        }
    )


def make_job(**overrides: Any) -> PublishJob:
    arguments: dict[str, Any] = {
        "post_id": POST_ID,
        "content": "hello world",
        "platforms": ("twitter", "linkedin"),
    }
    arguments.update(overrides)
    return create_publish_job(**arguments)


def publishes_everything(job: PublishJob) -> tuple[PlatformResult, ...]:
    return tuple(
        PlatformResult(platform=platform, status="published", duration_ms=120)
        for platform in job.platforms
    )


def fails_on(*failing: str):
    def publish(job: PublishJob) -> tuple[PlatformResult, ...]:
        return tuple(
            PlatformResult(
                platform=platform,
                status="failed" if platform in failing else "published",
                duration_ms=5,
                detail="simulated publish failure" if platform in failing else None,
            )
            for platform in job.platforms
        )

    return publish


def build(
    *,
    config: WorkerConfig | None = None,
    publisher: Any = publishes_everything,
    observability: WorkerObservability | None = None,
    depth_interval_seconds: float = 15.0,
) -> Harness:
    resolved = make_config() if config is None else config
    sink = RecordingSink()
    tracer: FakeTracerPort | None = None

    if observability is None:
        tracer = FakeTracerPort()
        observability = WorkerObservability(
            metrics=WorkerMetrics(env=resolved.observability.env, sink=sink),
            tracing=create_tracing(
                TracingOptions(
                    enabled=True,
                    service=resolved.observability.service,
                    env=resolved.observability.env,
                    version=None,
                ),
                lambda: tracer,
            ),
        )

    redis = FakeRedis()
    queue = RedisQueueClient(redis)
    clock = Clock()
    stream = io.StringIO()
    logger = create_logger(
        resolved,
        LoggerDeps(
            stream=stream,
            name=f"test-worker-observability-{id(stream)}",
            level="debug",
            trace_context=observability.tracing.trace_context,
        ),
    )
    loop = JobLoop(
        config=resolved,
        queue=queue,
        post_store=RedisPostStore(redis),
        logger=logger,
        deps=JobLoopDeps(
            publisher=publisher,
            sleep=lambda _seconds: None,
            observability=observability,
            depth_interval_seconds=depth_interval_seconds,
            monotonic=clock,
        ),
    )
    return Harness(
        loop=loop,
        queue=queue,
        redis=redis,
        sink=sink,
        tracer=tracer,
        stream=stream,
        clock=clock,
    )


# --- custom metrics for every outcome (Requirement 14.4) -----------------------


def test_counts_a_published_job_once_per_platform() -> None:
    harness = build()
    harness.queue.enqueue(make_job())

    harness.loop.run_once()

    assert harness.sink.tags(JOBS_PROCESSED) == [
        {"platform": "twitter", "status": "published", "env": "production"},
        {"platform": "linkedin", "status": "published", "env": "production"},
    ]
    assert harness.sink.named(JOBS_FAILED) == []


def test_records_each_platforms_duration() -> None:
    harness = build()
    harness.queue.enqueue(make_job())

    harness.loop.run_once()

    assert harness.sink.values(JOBS_DURATION) == [120, 120]


def test_counts_only_the_platform_that_failed_as_a_failure() -> None:
    # `failed / processed` is the design's worker-failure-rate monitor, so the two
    # have to be counted at the same granularity for the ratio to mean anything.
    harness = build(publisher=fails_on("linkedin"))
    harness.queue.enqueue(make_job())

    harness.loop.run_once()

    assert harness.sink.tags(JOBS_FAILED) == [
        {"platform": "linkedin", "status": "retrying", "env": "production"}
    ]
    assert [tags["platform"] for tags in harness.sink.tags(JOBS_PROCESSED)] == [
        "twitter",
        "linkedin",
    ]


def test_distinguishes_a_retry_from_a_job_that_gave_up() -> None:
    # A transient failure being retried and a job that exhausted its attempts are
    # different operational events, so they carry different `status` tags.
    harness = build(config=make_config(MAX_ATTEMPTS="2"), publisher=fails_on("twitter", "linkedin"))
    harness.queue.enqueue(make_job())

    harness.loop.run_once()
    harness.loop.run_once()

    assert [tags["status"] for tags in harness.sink.tags(JOBS_FAILED)] == [
        "retrying",
        "retrying",
        "failed",
        "failed",
    ]


def test_tags_a_partially_published_dead_letter_with_its_terminal_status() -> None:
    harness = build(config=make_config(MAX_ATTEMPTS="1"), publisher=fails_on("linkedin"))
    harness.queue.enqueue(make_job())

    harness.loop.run_once()

    assert {tags["status"] for tags in harness.sink.tags(JOBS_PROCESSED)} == {
        "partially_published"
    }
    assert harness.sink.tags(JOBS_FAILED) == [
        {"platform": "linkedin", "status": "partially_published", "env": "production"}
    ]


def test_counts_an_unparseable_payload_as_a_platformless_failure() -> None:
    # The attempt consumed a message and produced nothing publishable; a series that
    # skipped it would understate the failure rate.
    harness = build()
    harness.redis.lpush(KEYS.jobs, "not json at all")

    harness.loop.run_once()

    assert harness.sink.tags(JOBS_PROCESSED) == [
        {"platform": NO_PLATFORM, "status": "invalid", "env": "production"}
    ]
    assert harness.sink.tags(JOBS_FAILED) == [
        {"platform": NO_PLATFORM, "status": "invalid", "env": "production"}
    ]
    assert harness.sink.named(JOBS_DURATION) == []


def test_names_the_disposition_of_every_outcome_the_loop_can_produce() -> None:
    assert job_disposition(JobOutcome(job=None, dead_lettered=True)) == "invalid"
    job = make_job()
    assert job_disposition(JobOutcome(job=job, retried=True)) == "retrying"
    assert job_disposition(JobOutcome(job=job, post_status="published")) == "published"
    assert (
        job_disposition(JobOutcome(job=job, post_status="partially_published"))
        == "partially_published"
    )
    assert job_disposition(JobOutcome(job=job, post_status="failed")) == "failed"


# --- queue depth (Requirement 14.4) -------------------------------------------


def test_samples_queue_depth_alongside_the_poll() -> None:
    harness = build()
    harness.queue.enqueue(make_job())
    harness.queue.enqueue(make_job())

    harness.loop.run_once()

    # One job claimed, one still queued.
    assert harness.sink.named(QUEUE_DEPTH) == [
        (QUEUE_DEPTH, 1, {"backend": "redis", "env": "production"})
    ]


def test_samples_an_idle_queue_too_so_a_drained_backlog_is_visible() -> None:
    harness = build()

    assert harness.loop.run_once() is None

    assert harness.sink.values(QUEUE_DEPTH) == [0]


def test_does_not_sample_on_every_poll() -> None:
    # A sample is a round trip to the broker, and on SQS a billable API call.
    harness = build(depth_interval_seconds=15.0)

    harness.loop.run(max_iterations=3)
    harness.clock.advance(20.0)
    harness.loop.run_once()

    assert harness.sink.values(QUEUE_DEPTH) == [0, 0]


def test_shrugs_off_a_broker_that_cannot_answer_a_depth_query() -> None:
    harness = build()

    def broken_depth() -> int:
        raise ConnectionError("redis went away")

    # The depth query is the only thing broken: the receive that just returned is
    # better evidence about the broker than this call would be.
    harness.queue.depth = broken_depth  # type: ignore[method-assign]

    assert harness.loop.run_once() is None
    assert harness.find("queue depth sample failed")["error_type"] == "ConnectionError"


# --- the worker span continues the API's trace (Requirement 14.2) --------------


def test_starts_a_child_of_the_span_that_accepted_the_publish_request() -> None:
    harness = build()
    harness.queue.enqueue(make_job(trace_context=TRACE_HEADERS))

    harness.loop.run_once()

    assert harness.tracer is not None
    assert harness.tracer.activated == [TRACE_HEADERS]
    span = harness.tracer.span
    assert span.name == JOB_SPAN_NAME
    assert span.parent_trace_id == TRACE_ID
    assert span.finished is True


def test_starts_a_root_span_when_the_api_had_tracing_off() -> None:
    harness = build()
    harness.queue.enqueue(make_job())  # trace_context defaults to {}

    harness.loop.run_once()

    assert harness.tracer is not None
    assert harness.tracer.activated == []
    assert harness.tracer.span.parent_trace_id is None


def test_tags_the_span_with_the_job_identity_and_its_outcome() -> None:
    harness = build(publisher=fails_on("linkedin"))
    job = make_job(trace_context=TRACE_HEADERS)
    harness.queue.enqueue(job)

    harness.loop.run_once()

    assert harness.tracer is not None
    assert harness.tracer.span.tags == {
        "queue.backend": "redis",
        "job.id": job.job_id,
        "post.id": job.post_id,
        "job.attempt": 1,
        "job.platforms": "twitter,linkedin",
        "job.status": "retrying",
        "job.duration_ms": 10,
        "job.next_attempt": 2,
    }
    assert harness.tracer.span.resource == "twitter,linkedin"


def test_spans_a_payload_that_never_parsed_so_the_dead_letter_is_traceable() -> None:
    harness = build()
    harness.redis.lpush(KEYS.jobs, "not json at all")

    harness.loop.run_once()

    assert harness.tracer is not None
    assert harness.tracer.span.tags["job.status"] == "invalid"
    assert harness.tracer.span.tags["job.dead_letter_reason"] == "unparseable_payload"


# --- logs correlate with traces (Requirements 3.5, 14.3) ----------------------


def test_logs_every_outcome_with_post_id_platform_results_attempt_and_duration() -> None:
    harness = build()
    job = make_job(trace_context=TRACE_HEADERS)
    harness.queue.enqueue(job)

    harness.loop.run_once()

    line = harness.find("job published")
    assert line["post_id"] == job.post_id
    assert line["attempt"] == 1
    assert line["duration_ms"] == 240
    assert [entry["platform"] for entry in line["platforms"]] == ["twitter", "linkedin"]
    assert [entry["status"] for entry in line["platforms"]] == ["published", "published"]


def test_stamps_the_outcome_line_with_the_trace_it_belongs_to() -> None:
    harness = build()
    harness.queue.enqueue(make_job(trace_context=TRACE_HEADERS))

    harness.loop.run_once()

    assert harness.find("job published")["dd"] == {"trace_id": TRACE_ID, "span_id": SPAN_ID}


def test_says_at_startup_whether_it_is_observing_itself() -> None:
    harness = build()

    harness.loop.run(max_iterations=0)

    line = harness.find("job loop started")
    assert line["observability_enabled"] is True
    assert line["tracing_enabled"] is True


# --- the switch, off (Requirement 14.6) ---------------------------------------


def disabled() -> Harness:
    return build(observability=INERT_OBSERVABILITY)


def test_processes_a_job_identically_with_observability_disabled() -> None:
    # The switch changes what is recorded, never what the worker does.
    on = build()
    off = disabled()
    for harness in (on, off):
        harness.queue.enqueue(make_job(trace_context=TRACE_HEADERS))

    outcomes = [harness.loop.run_once() for harness in (on, off)]

    assert outcomes[0] is not None and outcomes[1] is not None
    # Job ids differ between the two envelopes, so the comparison is on everything
    # else the loop decided.
    assert outcomes[0].post_status == outcomes[1].post_status == "published"
    assert outcomes[0].results == outcomes[1].results
    assert outcomes[0].duration_ms == outcomes[1].duration_ms
    assert outcomes[0].acked == outcomes[1].acked is True
    assert on.record() | {"updated_at": ""} == off.record() | {"updated_at": ""}


def test_retries_and_dead_letters_identically_with_observability_disabled() -> None:
    config = make_config(MAX_ATTEMPTS="2")
    on = build(config=config, publisher=fails_on("twitter", "linkedin"))
    off = build(
        config=config, publisher=fails_on("twitter", "linkedin"), observability=INERT_OBSERVABILITY
    )
    for harness in (on, off):
        harness.queue.enqueue(make_job())
        harness.loop.run_once()
        harness.loop.run_once()

    assert on.record()["status"] == off.record()["status"] == "failed"
    assert len(on.redis.contents(KEYS.dead_letter)) == len(off.redis.contents(KEYS.dead_letter))
    assert on.redis.contents(KEYS.jobs) == off.redis.contents(KEYS.jobs) == []


def test_never_asks_the_broker_for_a_queue_depth_when_disabled() -> None:
    # "Inert" means the work is not done, not that its result is discarded.
    off = disabled()
    off.queue.enqueue(make_job())

    off.loop.run(max_iterations=2)

    assert "llen" not in off.commands()


def test_writes_no_trace_fields_when_disabled() -> None:
    off = disabled()
    off.queue.enqueue(make_job(trace_context=TRACE_HEADERS))

    off.loop.run_once()

    for line in off.lines():
        assert "dd" not in line
        # The fields Requirement 14.3 always demands are still there.
        assert line["service"] == "publishhub-worker"
        assert line["env"] == "production"


def test_the_startup_line_says_observability_is_off() -> None:
    off = disabled()

    off.loop.run(max_iterations=0)

    line = off.find("job loop started")
    assert line["observability_enabled"] is False
    assert line["tracing_enabled"] is False


# --- the process, end to end --------------------------------------------------


def test_a_worker_started_with_the_switch_off_runs_with_tracing_inert() -> None:
    # Requirement 14.6 at the process level: the bootstrap that runs on the first
    # import of the entrypoint installed no tracer, so nothing was patched and the
    # worker consumed a job anyway.
    #
    # `sys.modules` is deliberately not consulted. `ddtrace` ships a pytest plugin,
    # so once the package is installed the *test runner* imports it before any test
    # runs; the honest assertion is that this worker's tracing seam is the inert one.
    redis = FakeRedis()
    stream = io.StringIO()
    environment = {
        "POLL_WAIT_SECONDS": "0",
        "SIMULATE_LATENCY_MS": "0",
        "OBSERVABILITY_ENABLED": "false",
    }
    config = load_config(environment)

    exit_code = run_worker(
        config,
        RuntimeDeps(
            env=environment,
            logger_deps=LoggerDeps(stream=stream, name="test-worker-observability-process"),
            install_signals=False,
            create_redis=lambda _url: redis,  # type: ignore[arg-type]
            create_queue=lambda _queue_config: RedisQueueClient(redis),
            readiness=_NoReadiness(),
            max_iterations=1,
            sleep=lambda _seconds: None,
        ),
    )

    assert exit_code == EXIT_OK
    assert bootstrap.tracing is INERT_TRACING
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    starting = next(line for line in lines if line["msg"] == "worker starting")
    assert starting["observability_enabled"] is False
    assert starting["tracing_enabled"] is False


def test_the_process_builds_its_observability_from_configuration() -> None:
    # With the switch on, the entrypoint hands the loop a live metrics client; the
    # tracer stays inert here because the bootstrap it comes from ran with the
    # switch off in this test process.
    config = load_config({"OBSERVABILITY_ENABLED": "true", "DD_ENV": "staging"})

    observability = create_observability(config, sink=RecordingSink())

    assert observability.enabled is True
    assert observability.metrics.enabled is True


class _NoReadiness:
    """A readiness marker that touches no filesystem."""

    path = "(none)"

    def mark_ready(self) -> None:
        return None

    def mark_unready(self) -> None:
        return None
