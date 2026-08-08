"""
Envelope codec: serialize, parse, and validate the `PublishJob` message.

The rules here are the ones written down in `docs/message-schema.md` and
asserted by `contracts/publish-job.v1.fixture.json`. Both languages read that
fixture in their test suites, so the two implementations cannot drift
(Requirement 5.6). This module mirrors `apps/api/src/queue/publish-job.ts`
rule for rule, in the same order.

Validation never raises on bad input from the queue: it returns a reason, so the
caller can dead-letter the message instead of crash-looping on it
(Requirement 3.4).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .types import SCHEMA_VERSION, DeadLetterReason, Platform, PublishJob

CONTENT_MIN_LENGTH = 1
CONTENT_MAX_LENGTH = 5000

PLATFORM_ALLOW_LIST: tuple[Platform, ...] = ("twitter", "linkedin", "mastodon", "bluesky")

# Pattern text is identical to the TypeScript side and to
# `constraints.patterns` in the shared fixture; the contract test asserts that.
JOB_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
POST_ID_PATTERN = re.compile(r"^post_[0-9A-HJKMNP-TV-Z]{26}$")
ENQUEUED_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

#: Field order used when serializing. Key order is not part of the contract, but
#: a stable order keeps fixtures and log lines diffable.
FIELD_ORDER: tuple[str, ...] = (
    "schema_version",
    "job_id",
    "post_id",
    "content",
    "platforms",
    "attempt",
    "enqueued_at",
    "trace_context",
)


@dataclass(frozen=True, slots=True)
class ParseResult:
    """
    Outcome of decoding a queue payload. `ok` decides which of the other
    attributes carry meaning: `job` when it parsed, `reason` and `detail` when
    it did not.
    """

    ok: bool
    job: PublishJob | None = None
    reason: DeadLetterReason | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class JobDescription:
    """Fields useful for structured logs, tolerant of a payload that never parsed."""

    job_id: str | None
    post_id: str | None
    attempt: int | None


def character_length(value: str) -> int:
    """
    Length in Unicode code points. Python's `len` already counts code points, so
    this is a thin wrapper — it exists because the TypeScript side needs an
    explicit conversion to get the same number, and naming the concept in both
    languages is what keeps the 5000-character bound meaning one thing.
    """
    return len(value)


def is_platform(value: object) -> bool:
    return isinstance(value, str) and value in PLATFORM_ALLOW_LIST


def _describe(value: object) -> str:
    """JSON rendering of a rejected value, for the failure detail."""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


def _invalid(reason: DeadLetterReason, detail: str) -> ParseResult:
    return ParseResult(ok=False, reason=reason, detail=detail)


def _as_integer(value: object) -> int | None:
    """
    Integer value of a JSON number, or `None` when it is not an integer.

    `bool` is excluded deliberately: it is a subclass of `int` in Python, so
    without this guard `schema_version: true` would compare equal to `1` and a
    nonsense payload would pass. A float is accepted when it has no fractional
    part, matching `Number.isInteger` on the TypeScript side.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def validate_publish_job(value: object) -> ParseResult:
    """
    Validate an already-decoded value against version 1 of the envelope.

    Unknown top-level fields are ignored rather than rejected, so a rolling
    deploy where a newer producer adds a field stays safe. The returned job
    contains only the known fields.
    """
    if not isinstance(value, Mapping):
        return _invalid("unparseable_payload", "payload is not a JSON object")

    # Checked first: without a version we do not know which shape to expect, and
    # guessing is worse than dead-lettering.
    raw_version = value.get("schema_version")
    if _as_integer(raw_version) != SCHEMA_VERSION:
        return _invalid(
            "unknown_schema_version",
            f"schema_version must be {SCHEMA_VERSION}, received {_describe(raw_version)}",
        )

    job_id = value.get("job_id")
    if not isinstance(job_id, str) or JOB_ID_PATTERN.fullmatch(job_id) is None:
        return _invalid("schema_validation_failed", "job_id must be a lowercase UUID v4 string")

    post_id = value.get("post_id")
    if not isinstance(post_id, str) or POST_ID_PATTERN.fullmatch(post_id) is None:
        return _invalid(
            "schema_validation_failed",
            "post_id must match ^post_[0-9A-HJKMNP-TV-Z]{26}$",
        )

    content = value.get("content")
    if not isinstance(content, str):
        return _invalid("schema_validation_failed", "content must be a string")
    if len(content.strip()) < CONTENT_MIN_LENGTH:
        return _invalid("schema_validation_failed", "content must not be blank")
    if character_length(content) > CONTENT_MAX_LENGTH:
        return _invalid(
            "schema_validation_failed",
            f"content must be at most {CONTENT_MAX_LENGTH} characters",
        )

    platforms = value.get("platforms")
    # `str` is excluded explicitly: it is a Sequence, and accepting it would turn
    # "twitter" into four single-character targets.
    if not isinstance(platforms, Sequence) or isinstance(platforms, (str, bytes)):
        return _invalid("schema_validation_failed", "platforms must be a non-empty array")
    if len(platforms) == 0:
        return _invalid("schema_validation_failed", "platforms must be a non-empty array")
    unsupported = [platform for platform in platforms if not is_platform(platform)]
    if unsupported:
        rendered = ", ".join(_describe(platform) for platform in unsupported)
        return _invalid(
            "schema_validation_failed",
            f"platforms contains unsupported target(s): {rendered}",
        )
    if len(set(platforms)) != len(platforms):
        return _invalid("schema_validation_failed", "platforms must not contain duplicates")

    attempt = _as_integer(value.get("attempt"))
    if attempt is None or attempt < 1:
        return _invalid("schema_validation_failed", "attempt must be an integer >= 1")

    enqueued_at = value.get("enqueued_at")
    if not isinstance(enqueued_at, str) or ENQUEUED_AT_PATTERN.fullmatch(enqueued_at) is None:
        return _invalid(
            "schema_validation_failed",
            "enqueued_at must be UTC RFC 3339 with millisecond precision, "
            "e.g. 2026-08-07T10:00:00.000Z",
        )

    trace_context = value.get("trace_context")
    if not isinstance(trace_context, Mapping):
        return _invalid(
            "schema_validation_failed",
            "trace_context must be an object; send {} when tracing is off, never null",
        )
    for key, header_value in trace_context.items():
        if not isinstance(key, str) or not isinstance(header_value, str):
            return _invalid(
                "schema_validation_failed",
                f"trace_context.{key} must be a string",
            )

    return ParseResult(
        ok=True,
        job=PublishJob(
            schema_version=SCHEMA_VERSION,
            job_id=job_id,
            post_id=post_id,
            content=content,
            platforms=tuple(platforms),
            attempt=attempt,
            enqueued_at=enqueued_at,
            trace_context=dict(trace_context),
        ),
    )


def parse_publish_job(raw: str) -> ParseResult:
    """Decode queue bytes into a validated job, or report why it cannot be used."""
    try:
        decoded = json.loads(raw)
    except ValueError as error:  # json.JSONDecodeError is a ValueError
        return _invalid("unparseable_payload", f"payload is not valid JSON: {error}")
    return validate_publish_job(decoded)


def serialize_publish_job(job: PublishJob) -> str:
    """
    Encode a job as the UTF-8 JSON text written to either backend.

    Compact separators and `ensure_ascii=False` so the bytes match what
    `JSON.stringify` produces on the API side: no padding whitespace, and
    non-ASCII content transmitted as UTF-8 rather than `\\uXXXX` escapes.
    """
    payload: dict[str, Any] = {
        "schema_version": job.schema_version,
        "job_id": job.job_id,
        "post_id": job.post_id,
        "content": job.content,
        "platforms": list(job.platforms),
        "attempt": job.attempt,
        "enqueued_at": job.enqueued_at,
        "trace_context": dict(job.trace_context),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def format_enqueued_at(moment: datetime | None = None) -> str:
    """RFC 3339 UTC with millisecond precision — the `enqueued_at` format."""
    when = datetime.now(timezone.utc) if moment is None else moment
    # A naive datetime is read as UTC rather than as local time: guessing the
    # local zone here would silently produce a wrong timestamp.
    utc = when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def create_publish_job(
    *,
    post_id: str,
    content: str,
    platforms: Sequence[Platform],
    attempt: int = 1,
    job_id: str | None = None,
    enqueued_at: str | None = None,
    trace_context: Mapping[str, str] | None = None,
) -> PublishJob:
    """
    Build a valid envelope, raising if the result would not be valid. Producing
    a malformed message is a programming error on this side of the queue, unlike
    receiving one, which is a runtime condition handled by dead-lettering.

    `attempt` defaults to 1; the worker passes an incremented value when
    re-enqueueing. `job_id` defaults to a fresh UUID v4 and is kept stable across
    retries by the caller. `enqueued_at` defaults to now and is refreshed on
    every enqueue, including retries. `trace_context` defaults to `{}`, meaning
    tracing is off and the worker starts a root span.
    """
    candidate = {
        "schema_version": SCHEMA_VERSION,
        "job_id": str(uuid.uuid4()) if job_id is None else job_id,
        "post_id": post_id,
        "content": content,
        "platforms": tuple(platforms),
        "attempt": attempt,
        "enqueued_at": format_enqueued_at() if enqueued_at is None else enqueued_at,
        "trace_context": {} if trace_context is None else dict(trace_context),
    }

    result = validate_publish_job(candidate)
    if not result.ok or result.job is None:
        raise ValueError(f"cannot create PublishJob: {result.detail}")
    return result.job


def describe_job(job: PublishJob | None) -> JobDescription:
    """Identity fields for structured logs, safe on a payload that never parsed."""
    if job is None:
        return JobDescription(job_id=None, post_id=None, attempt=None)
    return JobDescription(job_id=job.job_id, post_id=job.post_id, attempt=job.attempt)
