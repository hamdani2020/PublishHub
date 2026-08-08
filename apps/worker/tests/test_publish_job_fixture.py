"""
Contract test: the Python envelope implementation against the shared fixture both
languages read (Requirement 5.6).

These assertions are the drift alarm. If the fixture and this implementation
disagree, one of them changed without the other, and `docs/message-schema.md` says
they move together. The TypeScript mirror of this file is
`apps/api/src/queue/publish-job.fixture.test.ts`.
"""

from __future__ import annotations

import dataclasses

import pytest

from publishhub_worker.queue.publish_job import (
    CONTENT_MAX_LENGTH,
    CONTENT_MIN_LENGTH,
    ENQUEUED_AT_PATTERN,
    FIELD_ORDER,
    JOB_ID_PATTERN,
    PLATFORM_ALLOW_LIST,
    POST_ID_PATTERN,
    parse_publish_job,
    serialize_publish_job,
    validate_publish_job,
)
from publishhub_worker.queue.testing import load_publish_job_fixture, serialize_fixture_message
from publishhub_worker.queue.types import SCHEMA_VERSION, PublishJob

FIXTURE = load_publish_job_fixture()
CONSTRAINTS = FIXTURE["constraints"]
CANONICAL = FIXTURE["canonical"]


# --- fixture agreement --------------------------------------------------------


def test_fixture_describes_the_schema_version_this_build_implements() -> None:
    assert FIXTURE["schema_version"] == SCHEMA_VERSION


def test_fixture_matches_the_constraints_the_implementation_enforces() -> None:
    assert CONSTRAINTS["content_min_length"] == CONTENT_MIN_LENGTH
    assert CONSTRAINTS["content_max_length"] == CONTENT_MAX_LENGTH
    assert tuple(CONSTRAINTS["platform_allow_list"]) == PLATFORM_ALLOW_LIST
    assert CONSTRAINTS["patterns"]["job_id"] == JOB_ID_PATTERN.pattern
    assert CONSTRAINTS["patterns"]["post_id"] == POST_ID_PATTERN.pattern
    assert CONSTRAINTS["patterns"]["enqueued_at"] == ENQUEUED_AT_PATTERN.pattern


def test_dataclass_carries_exactly_the_required_field_set() -> None:
    field_names = tuple(field.name for field in dataclasses.fields(PublishJob))
    assert field_names == tuple(FIXTURE["required_fields"]) == FIELD_ORDER


# --- canonical message --------------------------------------------------------


def test_canonical_message_round_trips_byte_for_byte() -> None:
    raw = serialize_fixture_message(CANONICAL)

    parsed = parse_publish_job(raw)

    assert parsed.ok, parsed.detail
    assert parsed.job is not None
    # The same bytes the API would have written, which is what makes a message
    # captured from either backend replayable into the other.
    assert serialize_publish_job(parsed.job) == raw


def test_canonical_message_parses_into_the_documented_values() -> None:
    parsed = parse_publish_job(serialize_fixture_message(CANONICAL))

    assert parsed.job is not None
    assert parsed.job.job_id == CANONICAL["job_id"]
    assert parsed.job.post_id == CANONICAL["post_id"]
    assert parsed.job.content == CANONICAL["content"]
    assert parsed.job.platforms == tuple(CANONICAL["platforms"])
    assert parsed.job.attempt == CANONICAL["attempt"]
    assert parsed.job.enqueued_at == CANONICAL["enqueued_at"]
    assert parsed.job.trace_context == CANONICAL["trace_context"]


# --- valid variants -----------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    FIXTURE["variants"],
    ids=[variant["name"] for variant in FIXTURE["variants"]],
)
def test_accepts_every_valid_variant(variant: dict) -> None:
    result = parse_publish_job(serialize_fixture_message(variant["message"]))

    assert result.ok, f"{variant['name']} should be accepted: {result.detail}"


def test_drops_unknown_top_level_fields_instead_of_rejecting_the_message() -> None:
    variant = next(
        entry for entry in FIXTURE["variants"] if entry["name"] == "unknown_field_forward_compat"
    )

    result = parse_publish_job(serialize_fixture_message(variant["message"]))

    assert result.ok
    assert result.job is not None
    assert not hasattr(result.job, "scheduled_for")
    assert "scheduled_for" not in serialize_publish_job(result.job)


# --- invalid messages ---------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    FIXTURE["invalid"],
    ids=[entry["name"] for entry in FIXTURE["invalid"]],
)
def test_reports_the_documented_dead_letter_reason(entry: dict) -> None:
    raw = entry["raw"] if "raw" in entry else serialize_fixture_message(entry["message"])

    result = parse_publish_job(raw)

    assert not result.ok, f"{entry['name']} should be rejected"
    assert result.reason == entry["reason"]
    assert result.reason in CONSTRAINTS["dead_letter_reasons"]
    assert result.detail
    # Never raises: the worker has to be able to dead-letter what it cannot read
    # rather than crash-loop on it (Requirement 3.4).


# --- content length bounds ----------------------------------------------------


def test_accepts_content_at_the_maximum_length() -> None:
    at_limit = {**CANONICAL, "content": "a" * CONSTRAINTS["content_max_length"]}

    assert validate_publish_job(at_limit).ok


def test_rejects_content_one_character_over_the_maximum() -> None:
    over_limit = {**CANONICAL, "content": "a" * (CONSTRAINTS["content_max_length"] + 1)}

    result = validate_publish_job(over_limit)

    assert not result.ok
    assert result.reason == "schema_validation_failed"


def test_counts_characters_rather_than_bytes_at_the_boundary() -> None:
    # Every emoji is one character but four UTF-8 bytes, so a byte-length check
    # would reject this valid message.
    emoji_count = CONSTRAINTS["content_max_length"]

    assert validate_publish_job({**CANONICAL, "content": "🚀" * emoji_count}).ok
    assert not validate_publish_job({**CANONICAL, "content": "🚀" * (emoji_count + 1)}).ok


def test_rejects_a_boolean_schema_version_that_would_compare_equal_to_one() -> None:
    # `True == 1` in Python; without an explicit guard this nonsense payload
    # would be accepted here and rejected by the TypeScript consumer.
    result = validate_publish_job({**CANONICAL, "schema_version": True})

    assert not result.ok
    assert result.reason == "unknown_schema_version"
