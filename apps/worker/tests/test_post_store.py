"""
Post record store tests — the worker's write side (Requirement 3.1).

The point of these is cross-service, not local: the worker writes into a hash the
API created and the API's query endpoints read back. So what is asserted is the
contract — the key name, the field names, and the encoding — because a drift here
shows up as a post that stays `queued` forever in the UI, with both services
looking correct in isolation.

Everything runs against the in-memory fake, so no test needs a Redis server.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from publishhub_worker.posts import (
    POST_STATUSES,
    TERMINAL_POST_STATUSES,
    PlatformResult,
    PostStoreKeys,
    RedisPostStore,
    encode_platform_results,
)
from publishhub_worker.queue import ENQUEUED_AT_PATTERN
from publishhub_worker.queue.testing import FakeRedis

POST_ID = "post_01HZX3QK7M9V4TDR8N2C5EAB6F"
RECORD_KEY = f"publishhub:post:{POST_ID}"

# The fields the API writes when it creates the record. Copied from
# `encodePostRecord` in apps/api/src/posts/post-store.ts.
API_WRITTEN_FIELDS = {
    "id": POST_ID,
    "content": "hello world",
    "platforms": '["twitter","linkedin"]',
    "status": "queued",
    "job_id": "3f2a9b0c-5d41-4e8b-9f27-6c1d0a8e5b73",
    "created_at": "2026-08-07T10:00:00.000Z",
    "updated_at": "2026-08-07T10:00:00.000Z",
}


@pytest.fixture()
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture()
def store(redis: FakeRedis) -> RedisPostStore:
    return RedisPostStore(redis)


def results() -> tuple[PlatformResult, ...]:
    return (
        PlatformResult(platform="twitter", status="published", duration_ms=501),
        PlatformResult(platform="linkedin", status="failed", duration_ms=12, detail="rate limited"),
    )


def test_writes_the_status_to_the_key_the_api_reads(
    store: RedisPostStore, redis: FakeRedis
) -> None:
    store.record_status(POST_ID, status="published", results=results())

    assert store.record_key(POST_ID) == RECORD_KEY
    assert redis.fields(RECORD_KEY)["status"] == "published"


def test_stamps_updated_at_in_the_one_timestamp_format_the_system_uses(
    store: RedisPostStore, redis: FakeRedis
) -> None:
    store.record_status(
        POST_ID,
        status="published",
        moment=datetime(2026, 8, 7, 10, 0, 30, 250_000, tzinfo=UTC),
    )

    updated_at = redis.fields(RECORD_KEY)["updated_at"]

    assert updated_at == "2026-08-07T10:00:30.250Z"
    assert ENQUEUED_AT_PATTERN.fullmatch(updated_at)


def test_defaults_updated_at_to_now(store: RedisPostStore, redis: FakeRedis) -> None:
    store.record_status(POST_ID, status="failed")

    assert ENQUEUED_AT_PATTERN.fullmatch(redis.fields(RECORD_KEY)["updated_at"])


def test_encodes_platform_results_as_json_omitting_absent_details(
    store: RedisPostStore, redis: FakeRedis
) -> None:
    store.record_status(POST_ID, status="partially_published", results=results())

    decoded = json.loads(redis.fields(RECORD_KEY)["platform_results"])

    assert decoded == [
        {"platform": "twitter", "status": "published", "duration_ms": 501},
        {
            "platform": "linkedin",
            "status": "failed",
            "duration_ms": 12,
            "detail": "rate limited",
        },
    ]


def test_writes_an_empty_result_list_rather_than_omitting_the_field(
    store: RedisPostStore, redis: FakeRedis
) -> None:
    store.record_status(POST_ID, status="failed")

    assert redis.fields(RECORD_KEY)["platform_results"] == "[]"


def test_leaves_every_field_the_api_wrote_untouched(
    store: RedisPostStore, redis: FakeRedis
) -> None:
    # The whole reason the record is a hash rather than a JSON blob: the worker
    # updates three fields without a read-modify-write that could lose the rest.
    redis.hset(RECORD_KEY, mapping=API_WRITTEN_FIELDS)

    store.record_status(POST_ID, status="published", results=results())

    fields = redis.fields(RECORD_KEY)
    assert fields["id"] == POST_ID
    assert fields["content"] == "hello world"
    assert fields["platforms"] == '["twitter","linkedin"]'
    assert fields["job_id"] == API_WRITTEN_FIELDS["job_id"]
    assert fields["created_at"] == API_WRITTEN_FIELDS["created_at"]
    # ...and the two it does own have moved on from what the API wrote.
    assert fields["status"] == "published"
    assert fields["updated_at"] != API_WRITTEN_FIELDS["updated_at"]


def test_honors_an_overridden_key_prefix_so_tests_can_be_isolated(redis: FakeRedis) -> None:
    scoped = RedisPostStore(redis, keys=PostStoreKeys(post_prefix="scoped:post:"))

    scoped.record_status(POST_ID, status="published")

    assert redis.fields(f"scoped:post:{POST_ID}")["status"] == "published"
    assert redis.fields(RECORD_KEY) == {}


def test_status_vocabulary_matches_the_api_and_names_the_terminal_ones() -> None:
    # Order and spelling copied from POST_STATUSES in
    # apps/api/src/posts/post-store.ts; a rename on either side has to fail here.
    assert POST_STATUSES == (
        "queued",
        "processing",
        "published",
        "partially_published",
        "failed",
    )
    assert TERMINAL_POST_STATUSES == ("published", "partially_published", "failed")
    assert set(TERMINAL_POST_STATUSES) <= set(POST_STATUSES)


def test_encodes_an_empty_result_sequence_as_an_empty_array() -> None:
    assert encode_platform_results(()) == "[]"
