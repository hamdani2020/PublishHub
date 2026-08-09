"""
Job processing loop tests (Requirements 3.1, 3.2, 3.3, 3.4).

Four behaviors carry these tasks. The success path: a queued job is claimed, each
platform is published, the terminal status lands on the post record the API reads,
and only then is the message acked. Idle behavior: an empty queue costs one
blocking receive per `POLL_WAIT_SECONDS` and nothing else — no sleep, no spin, no
work. Retry: a failed publish waits out an exponential backoff, comes back as a
fresh envelope with the next attempt number, and releases the original message.
And dead-lettering: a job that runs out of attempts, or a payload nobody can read,
leaves the queue with an explicit reason instead of blocking it.

The loop is driven through the real `RedisQueueClient` and `RedisPostStore` against
the in-memory fake, so the reliable-queue semantics and the Redis key layout are
exercised rather than mocked. No test needs Redis, AWS, or a network, and no test
spends a backoff wait: the loop's sleep is injected and recorded.

Graceful shutdown (task 4.4) and metrics and tracing (4.5) are out of scope here;
where this task leaves a seam for them, the test says so rather than asserting
finished behavior.
"""

from __future__ import annotations

import io
import json
import time
from typing import Any

import pytest

from publishhub_worker.config import WorkerConfig, load_config
from publishhub_worker.logging import LoggerDeps, WorkerLogger, create_logger
from publishhub_worker.posts import PlatformResult, RedisPostStore
from publishhub_worker.processing import JobLoop, JobLoopDeps
from publishhub_worker.queue import (
    DEFAULT_REDIS_QUEUE_KEYS,
    PublishJob,
    RedisQueueClient,
    SqsQueueClient,
    create_publish_job,
    parse_publish_job,
    serialize_publish_job,
)
from publishhub_worker.queue.testing import FakeRedis, FakeSqsPort

KEYS = DEFAULT_REDIS_QUEUE_KEYS
POST_ID = "post_01HZX3QK7M9V4TDR8N2C5EAB6F"
RECORD_KEY = f"publishhub:post:{POST_ID}"
POLL_WAIT_SECONDS = 7
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/000000000000/publishhub-jobs"


class FrozenClock:
    """Explicit time, so the reaper's staleness window is tested without sleeping."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Sink:
    """Captures what the loop logged, as parsed JSON objects."""

    def __init__(self, logger: WorkerLogger, stream: io.StringIO) -> None:
        self.logger = logger
        self._stream = stream

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self._stream.getvalue().splitlines() if line]

    def find(self, event: str) -> dict[str, Any]:
        matches = [line for line in self.lines() if line["msg"] == event]
        assert len(matches) == 1, f'expected one "{event}" line, got {len(matches)}'
        return matches[0]

    def events(self) -> list[str]:
        return [line["msg"] for line in self.lines()]


class Harness:
    """A wired loop plus the fakes behind it."""

    def __init__(
        self,
        *,
        config: WorkerConfig,
        loop: JobLoop,
        queue: RedisQueueClient,
        redis: FakeRedis,
        sink: Sink,
        clock: FrozenClock,
        sleeps: list[float],
    ) -> None:
        self.config = config
        self.loop = loop
        self.queue = queue
        self.redis = redis
        self.sink = sink
        self.clock = clock
        #: Every backoff wait the loop asked for, in order. The loop never blocks.
        self.sleeps = sleeps

    def record(self) -> dict[str, str]:
        return self.redis.fields(RECORD_KEY)

    def queued(self) -> list[PublishJob]:
        """Envelopes currently on the jobs list, parsed."""
        return [_parse(payload) for payload in self.redis.contents(KEYS.jobs)]

    def dead_lettered(self) -> list[str]:
        return self.redis.contents(KEYS.dead_letter)

    def platform_results(self) -> list[dict[str, Any]]:
        return json.loads(self.record()["platform_results"])

    def receive_timeouts(self) -> list[int]:
        return [
            call[3]
            for call in self.redis.calls
            if call[0] == "brpoplpush" and call[1] == KEYS.jobs
        ]


def make_job(**overrides: Any) -> PublishJob:
    arguments: dict[str, Any] = {
        "post_id": POST_ID,
        "content": "hello world",
        "platforms": ("twitter", "linkedin"),
    }
    arguments.update(overrides)
    return create_publish_job(**arguments)


def make_config(**overrides: str) -> WorkerConfig:
    return load_config(
        {
            "POLL_WAIT_SECONDS": str(POLL_WAIT_SECONDS),
            "SIMULATE_LATENCY_MS": "0",
            "SIMULATE_FAILURE_RATE": "0",
            "DD_ENV": "development",
            **overrides,
        }
    )


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


def raises(error: BaseException):
    def publish(_job: PublishJob) -> tuple[PlatformResult, ...]:
        raise error

    return publish


def _parse(payload: str) -> PublishJob:
    parsed = parse_publish_job(payload)
    assert parsed.ok and parsed.job is not None, parsed.detail
    return parsed.job


def build(
    *,
    config: WorkerConfig | None = None,
    publisher: Any = publishes_everything,
    should_continue: Any = None,
) -> Harness:
    resolved = make_config() if config is None else config
    redis = FakeRedis()
    clock = FrozenClock()
    queue = RedisQueueClient(redis, clock=clock)
    stream = io.StringIO()
    logger = create_logger(resolved, LoggerDeps(stream=stream, name="test-job-loop"))
    sleeps: list[float] = []
    deps = JobLoopDeps(
        publisher=publisher,
        sleep=sleeps.append,
        **({} if should_continue is None else {"should_continue": should_continue}),
    )
    loop = JobLoop(
        config=resolved,
        queue=queue,
        post_store=RedisPostStore(redis),
        logger=logger,
        deps=deps,
    )
    return Harness(
        config=resolved,
        loop=loop,
        queue=queue,
        redis=redis,
        sink=Sink(logger, stream),
        clock=clock,
        sleeps=sleeps,
    )


# --- the success path ---------------------------------------------------------


def test_writes_the_terminal_status_to_the_key_the_api_reads() -> None:
    harness = build()
    harness.queue.enqueue(make_job())

    outcome = harness.loop.run_once()

    assert outcome is not None
    assert outcome.published is True
    assert harness.record()["status"] == "published"
    assert harness.record()["updated_at"]


def test_records_a_result_for_every_requested_platform() -> None:
    harness = build()
    harness.queue.enqueue(make_job(platforms=("twitter", "linkedin", "bluesky")))

    harness.loop.run_once()

    assert harness.platform_results() == [
        {"platform": "twitter", "status": "published", "duration_ms": 120},
        {"platform": "linkedin", "status": "published", "duration_ms": 120},
        {"platform": "bluesky", "status": "published", "duration_ms": 120},
    ]


def test_acks_the_message_so_it_leaves_the_processing_list() -> None:
    harness = build()
    harness.queue.enqueue(make_job())

    harness.loop.run_once()

    assert harness.redis.contents(KEYS.jobs) == []
    assert harness.redis.contents(KEYS.processing) == []
    assert harness.redis.contents(KEYS.dead_letter) == []
    assert harness.redis.scores(KEYS.processing_claims) == {}


def test_records_the_status_before_acking_so_a_crash_cannot_lose_the_result() -> None:
    # A redelivered message rewrites the same terminal status, which is harmless.
    # Acking first would leave a published post stuck at `queued` forever.
    harness = build()
    harness.queue.enqueue(make_job())

    harness.loop.run_once()

    ordered = [
        call[0]
        for call in harness.redis.calls
        if call[:2] in {("hset", RECORD_KEY), ("lrem", KEYS.processing)}
    ]
    assert ordered == ["hset", "lrem"]


def test_logs_the_outcome_with_the_job_identity_and_platform_results() -> None:
    harness = build()
    job = make_job()
    harness.queue.enqueue(job)

    harness.loop.run_once()

    line = harness.sink.find("job published")
    assert line["job_id"] == job.job_id
    assert line["post_id"] == job.post_id
    assert line["attempt"] == 1
    assert line["duration_ms"] == 240
    assert [entry["platform"] for entry in line["platforms"]] == ["twitter", "linkedin"]


def test_serves_queued_jobs_first_in_first_out() -> None:
    harness = build()
    first = make_job(content="first")
    second = make_job(content="second")
    harness.queue.enqueue(first)
    harness.queue.enqueue(second)

    outcomes = [harness.loop.run_once(), harness.loop.run_once()]

    assert [outcome.job.content for outcome in outcomes if outcome and outcome.job] == [
        "first",
        "second",
    ]


def test_publishes_with_the_configured_simulation_when_no_publisher_is_injected() -> None:
    # Proves the default wiring, not the simulator: `SIMULATE_LATENCY_MS=0` keeps
    # it instant, and rate 0 keeps it deterministic.
    harness = build(publisher=None)
    harness.queue.enqueue(make_job())

    outcome = harness.loop.run_once()

    assert outcome is not None and outcome.published is True
    assert [result.status for result in outcome.results] == ["published", "published"]


# --- idle behavior (Requirement 3.2) ------------------------------------------


def test_waits_inside_receive_for_the_configured_poll_window() -> None:
    harness = build()

    assert harness.loop.run_once() is None
    assert harness.receive_timeouts() == [POLL_WAIT_SECONDS]


def test_an_idle_loop_costs_one_blocking_receive_per_iteration_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The whole of Requirement 3.2: the wait belongs inside `receive`, so the loop
    # itself must never sleep. A sleep here would be a busy-wait in disguise.
    def forbidden(_seconds: float) -> None:
        raise AssertionError("the job loop must not sleep; the wait belongs in receive()")

    monkeypatch.setattr(time, "sleep", forbidden)
    harness = build()

    harness.loop.run(max_iterations=3)

    assert harness.receive_timeouts() == [POLL_WAIT_SECONDS] * 3
    # Past the one-off startup reap, an idle iteration issues nothing but the
    # blocking receive: no post writes, no acks, no bookkeeping.
    calls = [call[0] for call in harness.redis.calls]
    assert calls[calls.index("brpoplpush") :] == ["brpoplpush"] * 3


def test_reports_no_outcome_and_writes_nothing_when_the_queue_is_empty() -> None:
    harness = build()

    assert harness.loop.run_once() is None
    assert harness.redis.hashes == {}


def test_keeps_the_idle_line_at_debug_so_a_parked_worker_is_quiet() -> None:
    # `DD_ENV=production` puts the logger at info, where the idle poll disappears.
    harness = build(config=make_config(DD_ENV="production"))

    harness.loop.run(max_iterations=2)

    assert "queue idle" not in harness.sink.events()
    assert harness.sink.find("job loop started")["poll_wait_seconds"] == POLL_WAIT_SECONDS


def test_records_the_idle_poll_at_debug_level_in_development() -> None:
    harness = build()

    harness.loop.run_once()

    line = harness.sink.find("queue idle")
    assert line["level"] == "debug"
    assert line["wait_seconds"] == POLL_WAIT_SECONDS


def test_stops_when_the_stop_condition_says_so() -> None:
    # The seam graceful shutdown (task 4.4) plugs a SIGTERM flag into.
    harness = build(should_continue=lambda: False)
    harness.queue.enqueue(make_job())

    harness.loop.run()

    assert harness.receive_timeouts() == []
    assert harness.redis.contents(KEYS.jobs) != []


# --- the startup reaper -------------------------------------------------------


def test_returns_a_message_abandoned_by_a_killed_worker_and_then_processes_it() -> None:
    harness = build()
    harness.queue.enqueue(make_job())
    claimed = harness.queue.receive(POLL_WAIT_SECONDS)
    assert claimed is not None  # the previous worker is killed here: no ack

    harness.clock.advance(301)
    harness.loop.run(max_iterations=1)

    assert harness.record()["status"] == "published"
    assert harness.redis.contents(KEYS.processing) == []
    assert harness.sink.find("reaped stale processing entries")["reclaimed"] == 1


def test_leaves_a_message_a_live_worker_is_still_processing() -> None:
    harness = build()
    harness.queue.enqueue(make_job())
    claimed = harness.queue.receive(POLL_WAIT_SECONDS)
    assert claimed is not None

    harness.clock.advance(30)
    result = harness.loop.reap_abandoned_jobs()

    assert result is not None and result.reclaimed == ()
    assert harness.redis.contents(KEYS.processing) == [claimed.raw]


def test_says_nothing_when_there_is_nothing_to_reap() -> None:
    harness = build()

    result = harness.loop.reap_abandoned_jobs()

    assert result is not None and result.reclaimed == ()
    assert "reaped stale processing entries" not in harness.sink.events()


def test_skips_the_reaper_on_sqs_where_the_broker_does_it() -> None:
    # SQS makes an unacked message visible again on its own when the visibility
    # timeout expires, so there is nothing for the reaper to do.
    config = make_config(QUEUE_BACKEND="sqs", SQS_QUEUE_URL=QUEUE_URL)
    port = FakeSqsPort(default_queue_url=QUEUE_URL)
    redis = FakeRedis()
    stream = io.StringIO()
    logger = create_logger(config, LoggerDeps(stream=stream, name="test-job-loop-sqs"))
    loop = JobLoop(
        config=config,
        queue=SqsQueueClient(port, queue_url=QUEUE_URL),
        post_store=RedisPostStore(redis),
        logger=logger,
        deps=JobLoopDeps(publisher=publishes_everything),
    )
    port.seed(serialize_publish_job(make_job()))

    assert loop.reap_abandoned_jobs() is None

    loop.run(max_iterations=1)

    assert port.receive_calls[0].wait_time_seconds == POLL_WAIT_SECONDS
    assert redis.fields(RECORD_KEY)["status"] == "published"
    assert len(port.deleted) == 1


# --- retry with exponential backoff (Requirement 3.3) -------------------------


def test_a_failed_platform_comes_back_as_the_next_attempt() -> None:
    harness = build(publisher=fails_on("linkedin"))
    job = make_job()
    harness.queue.enqueue(job)

    outcome = harness.loop.run_once()

    assert outcome is not None
    assert outcome.retried is True
    assert outcome.next_attempt == 2
    assert outcome.post_status is None  # not terminal yet, so the post stays queued
    assert harness.redis.hashes == {}
    requeued = harness.queued()
    assert [envelope.attempt for envelope in requeued] == [2]
    # Same job_id across attempts, so every attempt of one job correlates in logs.
    assert requeued[0].job_id == job.job_id
    assert requeued[0].content == job.content
    assert requeued[0].platforms == job.platforms


def test_the_original_message_leaves_the_processing_list_once_the_retry_is_queued() -> None:
    harness = build(publisher=fails_on("linkedin"))
    harness.queue.enqueue(make_job())

    harness.loop.run_once()

    assert harness.redis.contents(KEYS.processing) == []
    assert harness.redis.scores(KEYS.processing_claims) == {}
    assert harness.dead_lettered() == []


def test_queues_the_retry_before_releasing_the_original_so_a_crash_cannot_lose_it() -> None:
    harness = build(publisher=fails_on("linkedin"))
    harness.queue.enqueue(make_job())
    harness.redis.calls.clear()

    harness.loop.run_once()

    ordered = [
        call[0]
        for call in harness.redis.calls
        if call[:2] in {("lpush", KEYS.jobs), ("lrem", KEYS.processing)}
    ]
    assert ordered == ["lpush", "lrem"]


def test_waits_an_exponentially_growing_delay_between_attempts() -> None:
    # MAX_ATTEMPTS=3 means two retries, so two waits: 1s then 2s.
    harness = build(config=make_config(MAX_ATTEMPTS="3"), publisher=fails_on("twitter"))
    harness.queue.enqueue(make_job())

    harness.loop.run_once()
    harness.loop.run_once()

    assert harness.sleeps == [1.0, 2.0]
    assert [envelope.attempt for envelope in harness.queued()] == [3]


def test_refreshes_enqueued_at_on_the_retry_so_it_measures_this_attempts_wait() -> None:
    harness = build(publisher=fails_on("linkedin"))
    job = make_job(enqueued_at="2020-01-01T00:00:00.000Z")
    harness.queue.enqueue(job)

    harness.loop.run_once()

    assert harness.queued()[0].enqueued_at != job.enqueued_at


def test_logs_the_retry_with_the_next_attempt_and_the_wait() -> None:
    harness = build(publisher=fails_on("linkedin"))
    harness.queue.enqueue(make_job())

    harness.loop.run_once()

    line = harness.sink.find("job publish failed, retrying")
    assert line["attempt"] == 1
    assert line["next_attempt"] == 2
    assert line["max_attempts"] == 3
    assert line["retry_in_seconds"] == 1.0
    assert line["failed"] == ["linkedin"]


def test_retries_when_the_publisher_raises_rather_than_taking_the_process_down() -> None:
    harness = build(publisher=raises(RuntimeError("connection reset by peer")))
    harness.queue.enqueue(make_job())

    outcome = harness.loop.run_once()

    assert outcome is not None and outcome.retried is True
    assert [envelope.attempt for envelope in harness.queued()] == [2]
    assert harness.sink.find("job publish raised")["error_type"] == "RuntimeError"


def test_keeps_the_exception_message_out_of_the_record_the_api_serves() -> None:
    # `detail` is persisted and served to clients, so it carries the exception type
    # and nothing else: an error string can contain a host, a URL, or a credential.
    harness = build(
        config=make_config(MAX_ATTEMPTS="1"),
        publisher=raises(RuntimeError("redis://user:hunter2@cache:6379 refused")),
    )
    harness.queue.enqueue(make_job())

    harness.loop.run_once()

    details = [entry["detail"] for entry in harness.platform_results()]
    assert details == ["publish raised RuntimeError"] * 2
    assert "hunter2" not in json.dumps(harness.record())


# --- dead-lettering on attempt exhaustion (Requirements 3.3, 3.4) -------------


def test_dead_letters_the_job_once_the_attempts_run_out() -> None:
    harness = build(config=make_config(MAX_ATTEMPTS="2"), publisher=fails_on("twitter", "linkedin"))
    harness.queue.enqueue(make_job())

    harness.loop.run_once()
    outcome = harness.loop.run_once()

    assert outcome is not None
    assert outcome.dead_lettered is True
    assert outcome.dead_letter_reason == "max_attempts_exhausted"
    assert [_parse(payload).attempt for payload in harness.dead_lettered()] == [2]
    assert harness.redis.contents(KEYS.jobs) == []
    assert harness.redis.contents(KEYS.processing) == []
    assert harness.sleeps == [1.0]  # the second failure is not followed by a wait


def test_records_failed_when_no_platform_ever_published() -> None:
    harness = build(config=make_config(MAX_ATTEMPTS="1"), publisher=fails_on("twitter", "linkedin"))
    harness.queue.enqueue(make_job())

    outcome = harness.loop.run_once()

    assert outcome is not None and outcome.post_status == "failed"
    assert harness.record()["status"] == "failed"
    assert [entry["status"] for entry in harness.platform_results()] == ["failed", "failed"]


def test_records_partially_published_when_something_got_out() -> None:
    harness = build(config=make_config(MAX_ATTEMPTS="1"), publisher=fails_on("linkedin"))
    harness.queue.enqueue(make_job())

    outcome = harness.loop.run_once()

    assert outcome is not None and outcome.post_status == "partially_published"
    assert harness.record()["status"] == "partially_published"


def test_records_the_terminal_status_before_dead_lettering() -> None:
    # Same ordering rationale as the success path: a crash in between leaves the
    # message claimed, and the reaper brings it back for the same terminal write.
    harness = build(config=make_config(MAX_ATTEMPTS="1"), publisher=fails_on("twitter", "linkedin"))
    harness.queue.enqueue(make_job())
    harness.redis.calls.clear()

    harness.loop.run_once()

    ordered = [
        call[0]
        for call in harness.redis.calls
        if call[:2] in {("hset", RECORD_KEY), ("lpush", KEYS.dead_letter)}
    ]
    assert ordered == ["hset", "lpush"]


def test_max_attempts_of_one_dead_letters_without_ever_waiting() -> None:
    harness = build(config=make_config(MAX_ATTEMPTS="1"), publisher=fails_on("twitter"))
    harness.queue.enqueue(make_job())

    outcome = harness.loop.run_once()

    assert outcome is not None and outcome.dead_lettered is True
    assert outcome.retried is False
    assert harness.sleeps == []


def test_logs_the_dead_letter_with_its_reason_and_the_attempt_that_exhausted() -> None:
    harness = build(config=make_config(MAX_ATTEMPTS="1"), publisher=fails_on("twitter"))
    job = make_job()
    harness.queue.enqueue(job)

    harness.loop.run_once()

    line = harness.sink.find("job dead-lettered")
    assert line["reason"] == "max_attempts_exhausted"
    assert line["attempt"] == 1
    assert line["max_attempts"] == 1
    assert line["post_id"] == job.post_id
    assert line["status"] == "partially_published"


def test_a_dead_lettered_job_does_not_block_the_next_one() -> None:
    # Requirement 3.4: the queue keeps moving. The poison job is gone from the
    # jobs list, so the message behind it is served on the next receive.
    harness = build(config=make_config(MAX_ATTEMPTS="1"), publisher=fails_on("twitter"))
    harness.queue.enqueue(make_job(content="doomed"))
    harness.queue.enqueue(make_job(content="fine"))

    first = harness.loop.run_once()
    second = harness.loop.run_once()

    assert first is not None and first.dead_lettered is True
    assert second is not None and second.job is not None
    assert second.job.content == "fine"


def test_dead_letters_to_the_configured_sqs_queue_when_the_backend_is_sqs() -> None:
    # Nothing in the loop branches on the backend (Requirement 5.4): the same
    # exhaustion path sends the untouched payload to the DLQ and deletes the
    # original.
    dlq_url = f"{QUEUE_URL}-dlq"
    config = make_config(QUEUE_BACKEND="sqs", SQS_QUEUE_URL=QUEUE_URL, MAX_ATTEMPTS="1")
    port = FakeSqsPort(default_queue_url=QUEUE_URL)
    redis = FakeRedis()
    stream = io.StringIO()
    logger = create_logger(config, LoggerDeps(stream=stream, name="test-job-loop-sqs-dlq"))
    loop = JobLoop(
        config=config,
        queue=SqsQueueClient(port, queue_url=QUEUE_URL, dead_letter_queue_url=dlq_url),
        post_store=RedisPostStore(redis),
        logger=logger,
        deps=JobLoopDeps(publisher=fails_on("twitter", "linkedin"), sleep=lambda _seconds: None),
    )
    payload = serialize_publish_job(make_job())
    port.seed(payload)

    outcome = loop.run_once()

    assert outcome is not None and outcome.dead_letter_reason == "max_attempts_exhausted"
    assert [(sent.queue_url, sent.message_body) for sent in port.sent] == [(dlq_url, payload)]
    assert port.messages(QUEUE_URL) == []
    assert redis.fields(RECORD_KEY)["status"] == "failed"


# --- dead-lettering unreadable payloads (Requirement 3.4) ---------------------


def test_dead_letters_an_unparseable_payload_immediately() -> None:
    harness = build()
    harness.redis.lpush(KEYS.jobs, "not json at all")

    outcome = harness.loop.run_once()

    assert outcome is not None
    assert outcome.job is None
    assert outcome.dead_lettered is True
    assert outcome.dead_letter_reason == "unparseable_payload"
    assert outcome.retried is False
    # No retry, no wait: a payload nobody can parse will not parse next time either.
    assert harness.sleeps == []
    assert harness.dead_lettered() == ["not json at all"]
    assert harness.redis.contents(KEYS.processing) == []
    assert harness.sink.find("job payload dead-lettered")["reason"] == "unparseable_payload"


def test_dead_letters_an_unknown_schema_version_instead_of_guessing_the_shape() -> None:
    harness = build()
    payload = json.dumps({**json.loads(serialize_publish_job(make_job())), "schema_version": 2})
    harness.redis.lpush(KEYS.jobs, payload)

    outcome = harness.loop.run_once()

    assert outcome is not None
    assert outcome.dead_letter_reason == "unknown_schema_version"
    assert harness.dead_lettered() == [payload]
    line = harness.sink.find("job payload dead-lettered")
    assert line["reason"] == "unknown_schema_version"
    assert "schema_version must be 1" in line["detail"]


def test_dead_letters_a_payload_that_fails_field_validation() -> None:
    harness = build()
    payload = json.dumps({**json.loads(serialize_publish_job(make_job())), "platforms": []})
    harness.redis.lpush(KEYS.jobs, payload)

    outcome = harness.loop.run_once()

    assert outcome is not None
    assert outcome.dead_letter_reason == "schema_validation_failed"
    assert harness.dead_lettered() == [payload]


def test_writes_no_post_status_for_a_payload_with_no_readable_post_id() -> None:
    harness = build()
    harness.redis.lpush(KEYS.jobs, "{}")

    harness.loop.run_once()

    assert harness.redis.hashes == {}


def test_truncates_the_raw_payload_in_the_log_so_garbage_cannot_flood_it() -> None:
    harness = build()
    payload = "x" * 5000
    harness.redis.lpush(KEYS.jobs, payload)

    harness.loop.run_once()

    line = harness.sink.find("job payload dead-lettered")
    assert len(line["raw"]) < len(payload)
    assert line["raw_length"] == len(payload)
    # The payload itself is preserved on the dead-letter list, so it stays replayable.
    assert harness.dead_lettered() == [payload]


def test_a_poison_message_does_not_block_the_job_behind_it() -> None:
    harness = build()
    harness.redis.lpush(KEYS.jobs, "not json at all")
    harness.queue.enqueue(make_job())

    poison = harness.loop.run_once()
    good = harness.loop.run_once()

    assert poison is not None and poison.dead_lettered is True
    assert good is not None and good.published is True
    assert harness.record()["status"] == "published"
