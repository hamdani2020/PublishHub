"""
Redis backend unit tests against an in-memory fake (Requirement 5.2).

Two things matter here. The reliable-queue behavior: a claimed message lives in
the processing list until it is acked or dead-lettered, so nothing is lost if the
consumer dies mid-job. And the reaper: what a killed consumer left behind comes
back to the queue at the next worker startup (Requirement 3.4, spec task 4.2).

Mirrors `apps/api/src/queue/redis-queue-client.test.ts`, plus the reaper cases.
"""

from __future__ import annotations

import json

import pytest

from publishhub_worker.queue.publish_job import create_publish_job, serialize_publish_job
from publishhub_worker.queue.redis_queue_client import (
    DEFAULT_REDIS_QUEUE_KEYS,
    RedisQueueClient,
)
from publishhub_worker.queue.testing import FakeRedis
from publishhub_worker.queue.types import DeadLetterEvent, PublishJob, ReceivedJob, SqsJobHandle

KEYS = DEFAULT_REDIS_QUEUE_KEYS


def make_job(**overrides) -> PublishJob:
    arguments = {
        "post_id": "post_01HZX3QK7M9V4TDR8N2C5EAB6F",
        "content": "hello",
        "platforms": ("twitter",),
    }
    arguments.update(overrides)
    return create_publish_job(**arguments)


class FrozenClock:
    """Explicit time, so staleness is tested without sleeping."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture()
def dead_lettered() -> list[DeadLetterEvent]:
    return []


@pytest.fixture()
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture()
def client(
    redis: FakeRedis,
    dead_lettered: list[DeadLetterEvent],
    clock: FrozenClock,
) -> RedisQueueClient:
    return RedisQueueClient(redis, on_dead_letter=dead_lettered.append, clock=clock)


# --- enqueue and receive ------------------------------------------------------


def test_enqueues_the_serialized_envelope_onto_the_jobs_list(
    client: RedisQueueClient, redis: FakeRedis
) -> None:
    job = make_job()

    client.enqueue(job)

    assert redis.contents(KEYS.jobs) == [serialize_publish_job(job)]
    assert client.depth() == 1


def test_serves_messages_first_in_first_out(client: RedisQueueClient) -> None:
    client.enqueue(make_job(content="first"))
    client.enqueue(make_job(content="second"))

    first = client.receive(5)
    second = client.receive(5)

    assert first is not None and first.job is not None and first.job.content == "first"
    assert second is not None and second.job is not None and second.job.content == "second"


def test_moves_a_claimed_message_to_processing_rather_than_dropping_it(
    client: RedisQueueClient, redis: FakeRedis
) -> None:
    client.enqueue(make_job())

    received = client.receive(20)

    assert received is not None
    assert redis.contents(KEYS.jobs) == []
    assert redis.contents(KEYS.processing) == [received.raw]
    assert received.handle.backend == "redis"
    assert received.handle.payload == received.raw


def test_blocks_with_brpoplpush_for_a_positive_wait_and_polls_with_rpoplpush_for_zero(
    client: RedisQueueClient, redis: FakeRedis
) -> None:
    client.enqueue(make_job())
    client.receive(20)
    assert ("brpoplpush", KEYS.jobs, KEYS.processing, 20) in redis.calls

    client.enqueue(make_job())
    client.receive(0)
    assert ("rpoplpush", KEYS.jobs, KEYS.processing) in redis.calls


def test_truncates_a_fractional_wait_and_treats_a_negative_wait_as_no_wait(
    client: RedisQueueClient, redis: FakeRedis
) -> None:
    client.enqueue(make_job())
    client.receive(2.9)
    assert ("brpoplpush", KEYS.jobs, KEYS.processing, 2) in redis.calls

    client.enqueue(make_job())
    client.receive(-5)
    assert ("rpoplpush", KEYS.jobs, KEYS.processing) in redis.calls


def test_returns_none_when_the_queue_is_empty(client: RedisQueueClient) -> None:
    assert client.receive(1) is None


# --- ack and dead-letter ------------------------------------------------------


def test_removes_the_message_from_processing_on_ack(
    client: RedisQueueClient, redis: FakeRedis
) -> None:
    client.enqueue(make_job())
    received = client.receive(5)
    assert received is not None

    client.ack(received)

    assert redis.contents(KEYS.processing) == []
    assert redis.contents(KEYS.dead_letter) == []
    assert redis.scores(KEYS.processing_claims) == {}


def test_pushes_to_the_dead_letter_list_before_clearing_processing(
    client: RedisQueueClient, redis: FakeRedis
) -> None:
    client.enqueue(make_job())
    received = client.receive(5)
    assert received is not None

    client.dead_letter(received, "max_attempts_exhausted")

    assert redis.contents(KEYS.dead_letter) == [received.raw]
    assert redis.contents(KEYS.processing) == []

    # Push first, then remove: a crash between the two leaves a recoverable
    # duplicate, where the opposite order would lose the message outright.
    ordered = [
        call[0]
        for call in redis.calls
        if call[:2] in {("lpush", KEYS.dead_letter), ("lrem", KEYS.processing)}
    ]
    assert ordered == ["lpush", "lrem"]


def test_dead_letters_the_payload_unchanged_so_it_stays_replayable(
    client: RedisQueueClient, redis: FakeRedis
) -> None:
    job = make_job()
    client.enqueue(job)
    received = client.receive(5)
    assert received is not None

    client.dead_letter(received, "schema_validation_failed")

    assert redis.contents(KEYS.dead_letter) == [serialize_publish_job(job)]


def test_reports_the_dead_letter_reason_and_job_identity_for_logging(
    client: RedisQueueClient, dead_lettered: list[DeadLetterEvent]
) -> None:
    job = make_job()
    client.enqueue(job)
    received = client.receive(5)
    assert received is not None

    client.dead_letter(received, "max_attempts_exhausted")

    assert dead_lettered == [
        DeadLetterEvent(
            backend="redis",
            reason="max_attempts_exhausted",
            job_id=job.job_id,
            post_id=job.post_id,
            attempt=1,
            via_redrive_policy=False,
        )
    ]


# --- bad payloads -------------------------------------------------------------


def test_surfaces_an_unparseable_payload_instead_of_raising(
    client: RedisQueueClient, redis: FakeRedis, dead_lettered: list[DeadLetterEvent]
) -> None:
    redis.lpush(KEYS.jobs, '{"schema_version": 1, "job_id": "3f2a9b0c-5d41-4e8b')

    received = client.receive(5)

    assert received is not None
    assert received.job is None
    assert received.invalid_reason == "unparseable_payload"
    assert received.invalid_detail

    client.dead_letter(received, received.invalid_reason)

    assert redis.contents(KEYS.dead_letter) == [received.raw]
    assert dead_lettered[0].job_id is None


def test_reports_an_unknown_schema_version_as_such_not_as_a_validation_failure(
    client: RedisQueueClient, redis: FakeRedis
) -> None:
    payload = json.loads(serialize_publish_job(make_job()))
    payload["schema_version"] = 2
    redis.lpush(KEYS.jobs, json.dumps(payload))

    received = client.receive(5)

    assert received is not None
    assert received.invalid_reason == "unknown_schema_version"


# --- configuration and isolation ----------------------------------------------


def test_honors_overridden_key_names_so_tests_and_tenants_can_be_isolated(
    redis: FakeRedis,
) -> None:
    scoped = RedisQueueClient(redis, keys={"jobs": "scoped:jobs"})

    scoped.enqueue(make_job())

    assert len(redis.contents("scoped:jobs")) == 1
    assert redis.contents(KEYS.jobs) == []


def test_rejects_an_unknown_key_name_rather_than_silently_ignoring_it(
    redis: FakeRedis,
) -> None:
    with pytest.raises(TypeError, match="unknown Redis queue key name"):
        RedisQueueClient(redis, keys={"job": "typo:jobs"})


def test_rejects_a_handle_from_the_other_backend_rather_than_silently_no_oping(
    client: RedisQueueClient,
) -> None:
    foreign = ReceivedJob(
        raw="{}",
        handle=SqsJobHandle(message_id="m", receipt_handle="r"),
    )

    with pytest.raises(TypeError, match="not portable across backends"):
        client.ack(foreign)


def test_reports_queue_processing_and_dead_letter_depths(client: RedisQueueClient) -> None:
    client.enqueue(make_job())
    client.enqueue(make_job())
    received = client.receive(5)
    assert received is not None
    client.dead_letter(received, "max_attempts_exhausted")

    assert client.depth() == 1
    assert client.processing_depth() == 0
    assert client.dead_letter_depth() == 1


def test_closes_the_underlying_connection(client: RedisQueueClient, redis: FakeRedis) -> None:
    client.close()

    assert redis.closed is True


# --- the stale-processing reaper ----------------------------------------------


def test_reaper_returns_a_message_abandoned_by_a_killed_worker(
    client: RedisQueueClient, redis: FakeRedis, clock: FrozenClock
) -> None:
    client.enqueue(make_job())
    received = client.receive(20)
    assert received is not None
    # The worker is killed here: no ack, no dead-letter.

    clock.advance(301)
    result = client.reap_stale_processing(300)

    assert result.reclaimed == (received.raw,)
    assert redis.contents(KEYS.jobs) == [received.raw]
    assert redis.contents(KEYS.processing) == []
    assert redis.scores(KEYS.processing_claims) == {}


def test_reaper_leaves_a_message_a_live_worker_is_still_processing(
    client: RedisQueueClient, redis: FakeRedis, clock: FrozenClock
) -> None:
    client.enqueue(make_job())
    received = client.receive(20)
    assert received is not None

    clock.advance(30)
    result = client.reap_stale_processing(300)

    assert result.reclaimed == ()
    assert redis.contents(KEYS.processing) == [received.raw]
    assert redis.contents(KEYS.jobs) == []


def test_reaper_reclaims_a_reaped_message_only_once(
    client: RedisQueueClient, redis: FakeRedis, clock: FrozenClock
) -> None:
    client.enqueue(make_job())
    client.receive(20)
    clock.advance(301)

    first = client.reap_stale_processing(300)
    second = client.reap_stale_processing(300)

    assert len(first.reclaimed) == 1
    assert second.reclaimed == ()
    # Exactly one copy back on the queue: a second startup does not duplicate it.
    assert len(redis.contents(KEYS.jobs)) == 1


def test_reaper_does_nothing_when_the_processing_list_is_empty(
    client: RedisQueueClient, redis: FakeRedis
) -> None:
    result = client.reap_stale_processing(300)

    assert result.reclaimed == ()
    assert result.adopted == ()
    assert redis.contents(KEYS.jobs) == []


def test_reaper_adopts_an_untracked_entry_instead_of_reclaiming_it_immediately(
    client: RedisQueueClient, redis: FakeRedis, clock: FrozenClock
) -> None:
    # A consumer that does not maintain the claim index — the TypeScript client,
    # or a manual LPUSH during an incident — can leave a processing entry with no
    # claim record. Reclaiming it on sight could steal an in-flight job.
    orphan = serialize_publish_job(make_job())
    redis.lpush(KEYS.processing, orphan)

    adopting = client.reap_stale_processing(300)

    assert adopting.adopted == (orphan,)
    assert adopting.reclaimed == ()
    assert redis.contents(KEYS.processing) == [orphan]

    # One visibility window later it is treated like any other abandoned entry.
    clock.advance(301)
    reaping = client.reap_stale_processing(300)

    assert reaping.reclaimed == (orphan,)
    assert redis.contents(KEYS.jobs) == [orphan]


def test_reaper_returns_an_unparseable_payload_so_the_worker_can_dead_letter_it(
    client: RedisQueueClient, redis: FakeRedis, clock: FrozenClock
) -> None:
    # A poison message must not become a permanent processing-list resident: it
    # goes back to the queue, gets claimed, and is dead-lettered on parse.
    redis.lpush(KEYS.jobs, "not json at all")
    claimed = client.receive(20)
    assert claimed is not None and claimed.invalid_reason == "unparseable_payload"

    clock.advance(301)
    result = client.reap_stale_processing(300)

    assert result.reclaimed == ("not json at all",)
    assert redis.contents(KEYS.jobs) == ["not json at all"]


def test_reaper_prunes_a_claim_whose_payload_left_the_processing_list(
    client: RedisQueueClient, redis: FakeRedis, clock: FrozenClock
) -> None:
    payload = serialize_publish_job(make_job())
    redis.zadd(KEYS.processing_claims, {payload: clock.now})

    clock.advance(301)
    result = client.reap_stale_processing(300)

    assert result.pruned == (payload,)
    assert result.reclaimed == ()
    # Nothing invented onto the queue from bookkeeping alone.
    assert redis.contents(KEYS.jobs) == []
    assert redis.scores(KEYS.processing_claims) == {}


def test_reaper_uses_the_default_visibility_window_when_none_is_given(
    client: RedisQueueClient, redis: FakeRedis, clock: FrozenClock
) -> None:
    client.enqueue(make_job())
    client.receive(20)

    clock.advance(299)
    assert client.reap_stale_processing().reclaimed == ()

    clock.advance(2)
    assert len(client.reap_stale_processing().reclaimed) == 1


def test_reaper_reclaims_every_abandoned_entry_in_one_pass(
    client: RedisQueueClient, redis: FakeRedis, clock: FrozenClock
) -> None:
    for index in range(3):
        client.enqueue(make_job(content=f"job {index}"))
    claimed = [client.receive(20) for _ in range(3)]
    assert all(entry is not None for entry in claimed)

    clock.advance(301)
    result = client.reap_stale_processing(300)

    assert len(result.reclaimed) == 3
    assert redis.contents(KEYS.processing) == []
    assert len(redis.contents(KEYS.jobs)) == 3
