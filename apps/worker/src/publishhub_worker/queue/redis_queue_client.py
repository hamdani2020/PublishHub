"""
Redis backend — the free, offline-friendly local queue (Requirement 5.2).

| Operation     | Redis commands                                          |
|---------------|---------------------------------------------------------|
| `enqueue`     | `LPUSH publishhub:jobs`                                 |
| `receive`     | `BRPOPLPUSH jobs -> jobs:processing` (reliable queue)    |
| `ack`         | `LREM jobs:processing 1 <payload>`                      |
| `dead_letter` | `LPUSH jobs:dlq` then `LREM jobs:processing`             |
| `depth`       | `LLEN publishhub:jobs`                                  |

`BRPOPLPUSH` rather than `BRPOP`: a worker killed mid-job leaves the message in
the processing list instead of dropping it.

## The reaper

A message left behind by a killed worker sits in `publishhub:jobs:processing`
forever unless someone puts it back. `reap_stale_processing` is that someone: the
worker calls it at startup (spec task 4.2) and every entry older than the
visibility window goes back onto `publishhub:jobs` to be retried.

Redis list elements carry no metadata, so claim times live in a companion sorted
set, `publishhub:jobs:processing:claims`, scored by the Unix time of the claim.
`receive` adds the member, `ack` and `dead_letter` remove it, and the reaper reads
it. Two consequences worth knowing:

- The index is consumer-side bookkeeping, not part of the message contract
  (`docs/message-schema.md`), and the API — which only ever produces — neither
  writes nor reads it. A processing entry with no claim record is therefore
  possible; the reaper *adopts* it with the current time instead of reclaiming it
  immediately, so it becomes eligible one full visibility window later and an
  in-flight job is never stolen from a live worker.
- A sorted set holds one member per distinct payload, so two byte-identical
  payloads claimed concurrently share a claim record. Envelopes carry a fresh
  `job_id` per submission and a refreshed `enqueued_at` per re-enqueue, so
  byte-identical payloads in flight at the same time do not occur in practice.
"""

from __future__ import annotations

import dataclasses
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Callable, Protocol

from .publish_job import describe_job, parse_publish_job, serialize_publish_job
from .types import (
    DeadLetterEvent,
    DeadLetterListener,
    PublishJob,
    ReceivedJob,
    RedisJobHandle,
)


@dataclass(frozen=True, slots=True)
class RedisQueueKeys:
    """
    Key names this backend touches. `jobs` is also the `listName` the KEDA redis
    scaler watches, so renaming it means updating the ScaledObject too.
    """

    jobs: str
    processing: str
    dead_letter: str
    #: Sorted set of claim times backing the reaper. Consumer-side only.
    processing_claims: str


DEFAULT_REDIS_QUEUE_KEYS = RedisQueueKeys(
    jobs="publishhub:jobs",
    processing="publishhub:jobs:processing",
    dead_letter="publishhub:jobs:dlq",
    processing_claims="publishhub:jobs:processing:claims",
)

#: How long a claimed message may stay in the processing list before the reaper
#: treats it as abandoned. Comfortably longer than the worst-case single job
#: (a handful of simulated platform publishes plus backoff) so a live worker's
#: in-flight message is never reclaimed, and short enough that a crashed
#: worker's message is recovered by the next worker startup.
DEFAULT_VISIBILITY_TIMEOUT_SECONDS = 300.0


class RedisCommands(Protocol):
    """
    The narrow slice of Redis this client uses. A `redis.Redis` instance created
    with `decode_responses=True` satisfies it structurally, and the unit tests
    pass an in-memory fake, so no test needs a running Redis.
    """

    def lpush(self, name: str, *values: str) -> int: ...

    def brpoplpush(self, src: str, dst: str, timeout: int = 0) -> str | None: ...

    def rpoplpush(self, src: str, dst: str) -> str | None: ...

    def lrem(self, name: str, count: int, value: str) -> int: ...

    def llen(self, name: str) -> int: ...

    def lrange(self, name: str, start: int, end: int) -> list[str]: ...

    def zadd(self, name: str, mapping: Mapping[str, float]) -> int: ...

    def zrem(self, name: str, *values: str) -> int: ...

    def zrangebyscore(self, name: str, min: float | str, max: float | str) -> list[str]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ReapResult:
    """
    What one reaper pass did. Returned rather than only logged so the worker can
    report it at startup and tests can assert on it.
    """

    #: Payloads moved from the processing list back onto the jobs list.
    reclaimed: tuple[str, ...] = ()
    #: Untracked processing entries given a claim time now, so that they become
    #: reclaimable one visibility window from now instead of immediately.
    adopted: tuple[str, ...] = ()
    #: Claim records whose payload was no longer in the processing list. Nothing
    #: to return to the queue; the stale bookkeeping is dropped.
    pruned: tuple[str, ...] = ()


class RedisQueueClient:
    """Redis implementation of `QueueClient`."""

    def __init__(
        self,
        redis: RedisCommands,
        *,
        keys: RedisQueueKeys | Mapping[str, str] | None = None,
        on_dead_letter: DeadLetterListener | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._redis = redis
        self._keys = _resolve_keys(keys)
        self._on_dead_letter = on_dead_letter
        # Injectable so the reaper's staleness window is testable without sleeping.
        self._clock = time.time if clock is None else clock

    @property
    def keys(self) -> RedisQueueKeys:
        return self._keys

    def enqueue(self, job: PublishJob) -> None:
        self._redis.lpush(self._keys.jobs, serialize_publish_job(job))

    def receive(self, wait_seconds: float) -> ReceivedJob | None:
        timeout = max(0, math.trunc(wait_seconds))

        # `BRPOPLPUSH ... 0` blocks forever in Redis, whereas SQS treats a zero
        # wait as "return immediately". The non-blocking variant keeps both
        # backends behaving the same way for the same argument.
        if timeout == 0:
            payload = self._redis.rpoplpush(self._keys.jobs, self._keys.processing)
        else:
            payload = self._redis.brpoplpush(self._keys.jobs, self._keys.processing, timeout)

        if payload is None:
            return None

        # Record the claim time before parsing: even a payload we cannot read is
        # in the processing list now, and the reaper has to be able to see it.
        self._redis.zadd(self._keys.processing_claims, {payload: self._clock()})

        parsed = parse_publish_job(payload)
        handle = RedisJobHandle(payload=payload)

        if parsed.ok:
            return ReceivedJob(raw=payload, handle=handle, job=parsed.job)
        return ReceivedJob(
            raw=payload,
            handle=handle,
            job=None,
            invalid_reason=parsed.reason,
            invalid_detail=parsed.detail,
        )

    def ack(self, job: ReceivedJob) -> None:
        payload = self._processing_member(job)
        self._redis.lrem(self._keys.processing, 1, payload)
        self._redis.zrem(self._keys.processing_claims, payload)

    def dead_letter(self, job: ReceivedJob, reason: str) -> None:
        payload = self._processing_member(job)

        # Push before removing from `processing`: a crash between the two leaves
        # a duplicate in the dead-letter list, which is recoverable, where the
        # opposite order would lose the message outright.
        #
        # The dead-letter entry is the raw payload, byte-for-byte, so a message
        # can be replayed into either backend. Redis lists carry no per-entry
        # metadata, so the reason travels through `on_dead_letter` for the caller
        # to log (the SQS backend additionally attaches it as a message
        # attribute).
        self._redis.lpush(self._keys.dead_letter, payload)
        self._redis.lrem(self._keys.processing, 1, payload)
        self._redis.zrem(self._keys.processing_claims, payload)

        described = describe_job(job.job)
        self._emit(
            DeadLetterEvent(
                backend="redis",
                reason=reason,
                job_id=described.job_id,
                post_id=described.post_id,
                attempt=described.attempt,
                via_redrive_policy=False,
            )
        )

    def depth(self) -> int:
        return self._redis.llen(self._keys.jobs)

    def processing_depth(self) -> int:
        """Entries claimed but not yet acked. Used by the reaper and by logs."""
        return self._redis.llen(self._keys.processing)

    def dead_letter_depth(self) -> int:
        return self._redis.llen(self._keys.dead_letter)

    def reap_stale_processing(
        self,
        visibility_timeout_seconds: float = DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
        *,
        now: float | None = None,
    ) -> ReapResult:
        """
        Return abandoned `processing` entries to the jobs list.

        Called at worker startup (spec task 4.2). An entry is abandoned when its
        claim is older than `visibility_timeout_seconds`; an entry with no claim
        record is adopted with the current time rather than reclaimed, so a live
        worker mid-job is never robbed of its message.

        Safe to run while other workers are processing, and safe to run twice: a
        second pass over an already-reaped list finds nothing to do.
        """
        moment = self._clock() if now is None else now
        cutoff = moment - max(0.0, visibility_timeout_seconds)

        claims = self._keys.processing_claims
        tracked = set(self._redis.zrangebyscore(claims, "-inf", "+inf"))
        processing = self._redis.lrange(self._keys.processing, 0, -1)

        adopted: list[str] = []
        for payload in dict.fromkeys(processing):
            if payload not in tracked:
                self._redis.zadd(claims, {payload: moment})
                adopted.append(payload)

        pending = dict.fromkeys(processing)
        reclaimed: list[str] = []
        pruned: list[str] = []
        for payload in self._redis.zrangebyscore(claims, "-inf", cutoff):
            if payload not in pending:
                # Bookkeeping outlived the list entry, e.g. an ack that removed
                # the payload but not its claim. Nothing to return.
                self._redis.zrem(claims, payload)
                pruned.append(payload)
                continue

            # Same ordering rationale as `dead_letter`: a crash between the two
            # commands leaves a duplicate, which the retry path tolerates,
            # instead of losing the message.
            self._redis.lpush(self._keys.jobs, payload)
            self._redis.lrem(self._keys.processing, 1, payload)
            self._redis.zrem(claims, payload)
            reclaimed.append(payload)

        return ReapResult(
            reclaimed=tuple(reclaimed),
            adopted=tuple(adopted),
            pruned=tuple(pruned),
        )

    def close(self) -> None:
        self._redis.close()

    def _emit(self, event: DeadLetterEvent) -> None:
        if self._on_dead_letter is not None:
            self._on_dead_letter(event)

    def _processing_member(self, job: ReceivedJob) -> str:
        if job.handle.backend != "redis":
            raise TypeError(
                f'RedisQueueClient received a "{job.handle.backend}" handle; '
                "handles are not portable across backends"
            )
        return job.handle.payload


def _resolve_keys(keys: RedisQueueKeys | Mapping[str, str] | None) -> RedisQueueKeys:
    """Full key set, a partial override of the defaults, or the defaults."""
    if keys is None:
        return DEFAULT_REDIS_QUEUE_KEYS
    if isinstance(keys, RedisQueueKeys):
        return keys

    known = {field.name for field in dataclasses.fields(RedisQueueKeys)}
    unknown = sorted(set(keys) - known)
    if unknown:
        raise TypeError(f"unknown Redis queue key name(s): {', '.join(unknown)}")
    return dataclasses.replace(DEFAULT_REDIS_QUEUE_KEYS, **dict(keys))
