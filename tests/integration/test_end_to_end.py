"""
End-to-end integration test (spec task 6.2).

Two paths through the real stack, both starting at `POST /api/v1/publish` and
ending where a user or an operator would look:

1. A submitted post reaches a terminal status, written by the worker and read back
   through the API (Requirements 2.1, 3.1, 5.4).
2. A post whose every simulated publish fails ends up in the dead-letter list
   after its attempts are exhausted, without leaving the queue blocked
   (Requirement 3.4).

Requirement 5.4 — "switching backends requires only environment variable changes"
— is not asserted with a mock. It is what this suite *is*: the API and the worker
both run here configured with `QUEUE_BACKEND=redis` and nothing else, and neither
service's code appears in this file.

What these tests add over the unit suites is agreement. `apps/api` and
`apps/worker` each cover their own half against in-memory doubles, and both assert
the shared envelope fixture in `contracts/`. Neither can catch the API writing
`publishhub:post:<id>` while the worker reads a different key, one side encoding
`platforms` as a JSON array and the other as a comma-joined string, or a Redis
list name that only matches in the fixture. Those failures need two real processes
and one real Redis, which is what the `stack` fixture provides.
"""

from __future__ import annotations

import json
from typing import Any

from conftest import FAILING_WORKER_MAX_ATTEMPTS
from stack import TERMINAL_POST_STATUSES, ComposeStack, wait_for

#: The queue and its dead-letter destination, from the design's queue abstraction
#: table. Named here as the literals an operator would type into `redis-cli`,
#: deliberately not imported from either service: a rename on either side should
#: fail this test rather than follow it silently.
JOBS_KEY = "publishhub:jobs"
DLQ_KEY = "publishhub:jobs:dlq"


def _publish(stack: ComposeStack, content: str, platforms: list[str]) -> str:
    """Submit one post and return its id, asserting the 202 contract on the way."""
    status, body = stack.publish(content, platforms)

    assert status == 202, f"expected 202 Accepted from /api/v1/publish, got {status}: {body!r}"
    assert isinstance(body, dict), f"expected a JSON object body, got {body!r}"
    # `queued` and nothing else: the API's job is to accept and enqueue, and a
    # response claiming any further progress would be guessing at the worker's.
    assert body.get("status") == "queued", body
    post_id = body.get("id")
    assert isinstance(post_id, str) and post_id.startswith("post_"), body

    return post_id


def _await_terminal_record(
    stack: ComposeStack,
    post_id: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """
    Poll `GET /api/v1/posts/:id` until the worker has written a terminal status.

    Reading the outcome back through the API rather than out of Redis is the
    point: it proves the round trip a client actually depends on, including that
    the worker's partial `HSET` left the API's record readable.
    """

    def probe() -> dict[str, Any] | None:
        status, body = stack.post_record(post_id)
        if status != 200 or not isinstance(body, dict):
            return None
        return body if body.get("status") in TERMINAL_POST_STATUSES else None

    return wait_for(
        probe,
        timeout_seconds=timeout_seconds,
        description=f"post {post_id} to reach a terminal status",
        context=stack.diagnostics,
    )


def test_submitted_post_reaches_a_terminal_status(
    stack: ComposeStack,
    job_timeout_seconds: float,
) -> None:
    """
    The happy path: API accepts, worker publishes, API reports it.

    Validates: Requirements 2.1, 3.1, 5.4
    """
    content = "PublishHub integration test — the happy path."
    platforms = ["twitter", "linkedin"]

    post_id = _publish(stack, content, platforms)
    record = _await_terminal_record(stack, post_id, timeout_seconds=job_timeout_seconds)

    # `published` specifically, not merely terminal: the baseline worker runs with
    # `SIMULATE_FAILURE_RATE=0`, so anything else means a real failure rather than
    # a simulated one.
    assert record["status"] == "published", record
    # The record the worker finished is the record the API created — same id, same
    # body, same targets in the same order. This is the assertion that fails if the
    # two services ever disagree about the post hash's field encoding.
    assert record["id"] == post_id, record
    assert record["content"] == content, record
    assert list(record["platforms"]) == platforms, record
    # `updated_at` is written by the worker, `created_at` by the API, so ordering
    # between them shows the terminal status came from the worker rather than from
    # the API's initial write.
    assert record["updated_at"] >= record["created_at"], record

    assert stack.list_length(JOBS_KEY) == 0, (
        f"{JOBS_KEY} should be empty after the job was acked\n{stack.diagnostics()}"
    )


def test_forced_failure_job_lands_in_the_dead_letter_list(
    failing_worker: ComposeStack,
    job_timeout_seconds: float,
) -> None:
    """
    Attempts exhausted: terminal status recorded, message dead-lettered, queue free.

    Validates: Requirements 3.4, 2.1
    """
    stack = failing_worker
    # Measured rather than assumed to be zero: this stack may be a developer's,
    # and an earlier failed job in the list is not this test's business.
    dead_letters_before = stack.list_length(DLQ_KEY)

    post_id = _publish(stack, "PublishHub integration test — forced failure.", ["twitter"])
    record = _await_terminal_record(stack, post_id, timeout_seconds=job_timeout_seconds)

    # Requirement 3.1 stays true for a job that never succeeded: `failed` is
    # terminal, and a dead letter with the post left at `queued` would strand it.
    # `failed` rather than `partially_published` because every platform failed.
    assert record["status"] == "failed", record

    def probe() -> dict[str, Any] | None:
        for entry in stack.list_entries(DLQ_KEY):
            try:
                envelope = json.loads(entry)
            except json.JSONDecodeError:
                # A payload nobody could parse is exactly what the poison-message
                # path puts here, so it is a legitimate neighbour in this list —
                # just not the message this test is looking for.
                continue
            if isinstance(envelope, dict) and envelope.get("post_id") == post_id:
                return envelope
        return None

    envelope = wait_for(
        probe,
        timeout_seconds=job_timeout_seconds,
        description=f"post {post_id} to appear in {DLQ_KEY}",
        context=stack.diagnostics,
    )

    # The dead-lettered message is the last attempt's envelope, byte-for-byte, so
    # it can be replayed. `attempt` proves the retry path ran to exhaustion rather
    # than the message being dropped on first failure.
    assert envelope["attempt"] == FAILING_WORKER_MAX_ATTEMPTS, envelope
    assert envelope["schema_version"] == 1, envelope
    assert envelope["platforms"] == ["twitter"], envelope

    assert stack.list_length(DLQ_KEY) == dead_letters_before + 1, (
        f"expected exactly one new entry in {DLQ_KEY}\n{stack.diagnostics()}"
    )
    # "SHALL NOT block the queue": the dead letter left the main list empty, so the
    # next job is picked up immediately instead of queueing behind a hopeless one.
    assert stack.list_length(JOBS_KEY) == 0, (
        f"{JOBS_KEY} should be empty after the job was dead-lettered\n{stack.diagnostics()}"
    )
