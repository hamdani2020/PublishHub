"""
Post record store — the worker's write side (Requirement 3.1).

The record itself is created by the API (`apps/api/src/posts/post-store.ts`); the
worker only ever *finishes* one. When a job completes, the worker writes the
terminal status, the time it happened, and the per-platform results back onto the
same Redis hash the API's `GET /api/v1/posts` and `GET /api/v1/posts/:id` read.

| Key                      | Type | Holds                                    |
|--------------------------|------|------------------------------------------|
| `publishhub:post:<id>`   | hash | One post record, one field per attribute. |

The key layout and the field names are the contract between the two services, so
they are repeated here deliberately rather than derived: a rename on either side
has to be a visible edit on both. Post state lives in Redis in every environment
— only the *queue* swaps between Redis and SQS — which is the single-stateful-
dependency tradeoff recorded in the design.

Three deliberate choices:

- **A partial `HSET`, not a read-modify-write.** The worker owns exactly three
  fields (`status`, `updated_at`, `platform_results`) and touches nothing else, so
  it cannot clobber `content`, `platforms`, or `created_at` — which is the reason
  the API stores a hash rather than a JSON blob in the first place.
- **`platform_results` is JSON**, like the API's `platforms` field. It is a list
  of objects, and encoding a list as a list keeps it unambiguous.
- **The recent index is not touched.** `publishhub:posts:recent` is append-only
  and ordered by post id; a status change does not reorder it, so the worker has
  no reason to write to it. That also keeps the API as the only writer of the
  index, which is one fewer thing that can race.

A note on the missing-record case: `HSET` creates the hash if it is gone (Redis
here has no durability guarantee, and the API's record can be evicted). The
result is a hash carrying only the worker's three fields, which the API's
`decodePostRecord` reads as "no readable record" and skips rather than serving
half a post. Checking existence first would trade that harmless outcome for an
extra round trip and a race, so the write stays unconditional.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from ..queue import Platform, format_enqueued_at

#: Post lifecycle, in the same order and with the same spellings as
#: `POST_STATUSES` in `apps/api/src/posts/post-store.ts`. The API only ever
#: writes `queued`; every other value in this list is written by the worker.
POST_STATUSES: tuple[str, ...] = (
    "queued",
    "processing",
    "published",
    "partially_published",
    "failed",
)

PostStatus = Literal["queued", "processing", "published", "partially_published", "failed"]

#: Statuses that end a post's life. Reaching one of these is what Requirement 3.1
#: means by "record a terminal status for the post".
TERMINAL_POST_STATUSES: tuple[PostStatus, ...] = ("published", "partially_published", "failed")

#: Outcome of one simulated platform publish. Narrower than `PostStatus` on
#: purpose: a single platform either went out or it did not.
PlatformStatus = Literal["published", "failed"]


@dataclass(frozen=True, kw_only=True, slots=True)
class PlatformResult:
    """
    What happened for one platform. Persisted as an element of
    `platform_results`, and also carried in the structured log line for the job.
    """

    platform: Platform
    status: PlatformStatus
    #: Wall-clock duration of this platform's publish, milliseconds.
    duration_ms: int
    #: Human-readable explanation, present on failure. Never a stack trace: this
    #: value is served to clients through the post record.
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "published"


@dataclass(frozen=True, slots=True)
class PostStoreKeys:
    """
    Key names this store touches. Must match `DEFAULT_POST_STORE_KEYS` in
    `apps/api/src/posts/post-store.ts`.
    """

    #: Full record key is this prefix plus the post id.
    post_prefix: str = "publishhub:post:"


DEFAULT_POST_STORE_KEYS = PostStoreKeys()


class PostStoreCommands(Protocol):
    """
    The one Redis command the worker's store needs. A `redis.Redis` created with
    `decode_responses=True` satisfies it structurally, and the tests pass the
    in-memory fake from `publishhub_worker.queue.testing`, so no test needs a
    Redis server.

    Reads are deliberately absent: the worker writes a status it already knows,
    and adding `HGETALL` here would invite a read-modify-write cycle this store
    exists to avoid.
    """

    def hset(self, name: str, *, mapping: Mapping[str, str]) -> int: ...


class PostStore(Protocol):
    """The seam the job loop programs against."""

    def record_status(
        self,
        post_id: str,
        *,
        status: PostStatus,
        results: Sequence[PlatformResult] = (),
        moment: datetime | None = None,
    ) -> None:
        """Write the status, the update time, and the per-platform results."""


def encode_platform_results(results: Sequence[PlatformResult]) -> str:
    """
    JSON array of result objects, compact and UTF-8, matching how the API encodes
    `platforms`. `detail` is omitted when absent rather than written as `null`, so
    a successful result stays small.
    """
    payload = [
        {
            "platform": result.platform,
            "status": result.status,
            "duration_ms": result.duration_ms,
            **({} if result.detail is None else {"detail": result.detail}),
        }
        for result in results
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class RedisPostStore:
    """Redis implementation of `PostStore`."""

    __slots__ = ("_keys", "_redis")

    def __init__(
        self,
        redis: PostStoreCommands,
        *,
        keys: PostStoreKeys | None = None,
    ) -> None:
        self._redis = redis
        self._keys = DEFAULT_POST_STORE_KEYS if keys is None else keys

    @property
    def keys(self) -> PostStoreKeys:
        return self._keys

    def record_key(self, post_id: str) -> str:
        """`publishhub:post:<id>`. Exposed so a caller can name the key in a log."""
        return f"{self._keys.post_prefix}{post_id}"

    def record_status(
        self,
        post_id: str,
        *,
        status: PostStatus,
        results: Sequence[PlatformResult] = (),
        moment: datetime | None = None,
    ) -> None:
        self._redis.hset(
            self.record_key(post_id),
            mapping={
                "status": status,
                # The same formatter the message envelope uses for `enqueued_at`:
                # RFC 3339 UTC with millisecond precision, one timestamp format
                # across the whole system.
                "updated_at": format_enqueued_at(moment),
                "platform_results": encode_platform_results(results),
            },
        )
