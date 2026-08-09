"""
Queue abstraction — shared types (Python side).

The worker talks to `QueueClient`; nothing above this layer knows whether the
active backend is a Redis list or an Amazon SQS queue (Requirements 5.1, 5.4).
The wire format is defined once in `docs/message-schema.md` and mirrored by the
TypeScript `PublishJob` interface in `apps/api/src/queue/types.ts` and by
`PublishJob` here (Requirement 5.6).

This module is a deliberate mirror of `apps/api/src/queue/types.ts`: same names
in Python casing, same semantics. The one language-level difference is that the
Python client is synchronous, because `redis-py` and `boto3` are synchronous and
the worker is a single-job-at-a-time loop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

#: The only envelope version this build produces or accepts.
SCHEMA_VERSION = 1

#: Publish targets, lowercase and exact-match (docs/message-schema.md).
Platform = Literal["twitter", "linkedin", "mastodon", "bluesky"]

#: Terminal dead-letter reasons from docs/message-schema.md.
DeadLetterReason = Literal[
    "unparseable_payload",
    "unknown_schema_version",
    "schema_validation_failed",
    "max_attempts_exhausted",
]

QueueBackend = Literal["redis", "sqs"]


@dataclass(frozen=True, kw_only=True, slots=True)
class PublishJob:
    """
    The message envelope. Every field is required; a producer with nothing to
    say for `trace_context` sends `{}` rather than omitting the key or sending
    `None`.

    Keyword-only construction, so a field is never set by position and the
    argument order in this file is free to match the wire order.
    """

    #: Exactly `SCHEMA_VERSION`. Consumers dead-letter any other value.
    schema_version: int = SCHEMA_VERSION
    #: UUID v4, lowercase. Stable across retries so all attempts correlate.
    job_id: str
    #: `post_` + 26-char Crockford base32 ULID. Key of the Redis post record.
    post_id: str
    #: 1-5000 characters, not blank after stripping. Never truncated here.
    content: str
    #: Non-empty, no duplicates, submission order preserved. A tuple rather
    #: than a list so an envelope cannot be mutated after validation.
    platforms: tuple[Platform, ...]
    #: Delivery attempt number, one-based.
    attempt: int
    #: RFC 3339 UTC with millisecond precision, e.g. `2026-08-07T10:00:00.000Z`.
    enqueued_at: str
    #: Datadog propagation headers, `{}` when tracing is off. Treated as opaque.
    trace_context: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RedisJobHandle:
    """Claim handle for the Redis backend: the exact processing-list member."""

    payload: str
    backend: Literal["redis"] = "redis"


@dataclass(frozen=True, slots=True)
class SqsJobHandle:
    """Claim handle for the SQS backend: the receipt handle for this receive."""

    message_id: str
    receipt_handle: str
    backend: Literal["sqs"] = "sqs"


#: Backend-specific claim handle. The envelope itself never carries a receipt
#: handle or processing-list membership: those belong to the queue client.
JobHandle = RedisJobHandle | SqsJobHandle


@dataclass(frozen=True, slots=True)
class ReceivedJob:
    """
    A claimed message. `job` is `None` when the payload failed validation,
    because a consumer still has to dead-letter what it cannot parse — receive
    never raises on a bad payload, it reports it (Requirement 3.4).
    """

    #: Exactly the text that was on the queue, so a dead letter stays replayable.
    raw: str
    handle: JobHandle
    job: PublishJob | None = None
    invalid_reason: DeadLetterReason | None = None
    #: Human-readable explanation of the rejection, for logs.
    invalid_detail: str | None = None


@dataclass(frozen=True, slots=True)
class DeadLetterEvent:
    """Emitted whenever a message is dead-lettered, for structured logging."""

    backend: QueueBackend
    reason: str
    job_id: str | None
    post_id: str | None
    attempt: int | None
    #: True when the message was left in place for the SQS redrive policy to
    #: move rather than being sent to an explicitly configured dead-letter queue.
    via_redrive_policy: bool


#: Callback shape for `on_dead_letter`.
DeadLetterListener = Callable[[DeadLetterEvent], None]


@runtime_checkable
class QueueClient(Protocol):
    """The single interface the worker programs against (Requirement 5.4)."""

    def enqueue(self, job: PublishJob) -> None: ...

    def receive(self, wait_seconds: float) -> ReceivedJob | None:
        """
        Claim one message, blocking (Redis) or long-polling (SQS) for at most
        `wait_seconds`. Returns `None` when nothing arrived in that window.
        `wait_seconds <= 0` means "do not wait" in both backends.
        """

    def ack(self, job: ReceivedJob) -> None: ...

    def dead_letter(self, job: ReceivedJob, reason: str) -> None: ...

    def depth(self) -> int:
        """Pending message count, the same number KEDA scales on."""

    def close(self) -> None: ...


class QueueConfigError(Exception):
    """
    Raised at startup when the selected backend is unknown or a required
    setting for it is missing. `key` names the offending environment variable so
    the failure is actionable rather than opaque (Requirement 5.5).
    """

    def __init__(self, key: str, message: str) -> None:
        super().__init__(message)
        self.key = key
