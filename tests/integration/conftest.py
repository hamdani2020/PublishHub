"""
Fixtures for the end-to-end integration suite (spec task 6.2).

The suite runs against the real stack from `docker-compose.yaml`: a real Express
process, a real Redis, and a real Python worker in a container, connected over a
real network. Nothing here is faked, which is the whole point — the unit suites
already cover the logic against in-memory doubles, and what they cannot prove is
that the API and the worker agree about the message envelope, the Redis keys, and
the post record on the wire.

Two behaviors shape everything below.

**Skipping is a first-class outcome.** No Docker daemon means no stack, and a
machine without Docker has not broken PublishHub. Every reason the suite cannot
run turns into one clearly worded skip so `make test` stays green and the reader
still learns what to install or start. What is deliberately *not* here is a
fallback that quietly runs something weaker instead: a green integration suite
that never started a container would be a lie.

**The stack is shared, so teardown is conditional.** `docker-compose.yaml` fixes
the project name and publishes ports on loopback, so a developer running
`make dev-up` and this suite are talking about the same containers. When the
fixture starts the stack it stops it again; when it adopts a running one it leaves
it up and puts the worker back the way it found it. Either way the developer's
session survives the test run — apart from the two posts the suite submits, which
stay in the adopted stack's Redis along with the one dead-lettered message. That
is the honest cost of testing against the environment people actually run, and
Redis here is deliberately non-durable, so `make dev-down` clears it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping

import pytest

from stack import (
    BASELINE_WORKER_ENV,
    DEFAULT_API_BASE_URL,
    DEFAULT_JOB_TIMEOUT_SECONDS,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ComposeStack,
    docker_unavailable_reason,
)

#: Worker configuration for the dead-letter test (Requirement 3.4).
#:
#: `SIMULATE_FAILURE_RATE=1` makes every simulated platform publish fail, so the
#: outcome is deterministic rather than a coin flip that fails the suite one run
#: in twenty. `MAX_ATTEMPTS=2` means one retry — enough that the path under test
#: is genuinely "retried, then exhausted" and not "dead-lettered on first sight" —
#: and the default backoff makes that one retry a 1s wait rather than 1s + 2s.
FAILING_WORKER_ENV: Mapping[str, str] = {
    **BASELINE_WORKER_ENV,
    "SIMULATE_FAILURE_RATE": "1",
    "MAX_ATTEMPTS": "2",
}

#: `attempt` on the envelope that reaches the dead-letter list, given
#: `MAX_ATTEMPTS=2` above: the second delivery is the one that exhausts.
FAILING_WORKER_MAX_ATTEMPTS = int(FAILING_WORKER_ENV["MAX_ATTEMPTS"])


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise pytest.UsageError(f"{name} must be a number — received {raw!r}") from None
    if value <= 0:
        raise pytest.UsageError(f"{name} must be greater than 0 — received {raw!r}")
    return value


@pytest.fixture(scope="session")
def job_timeout_seconds() -> float:
    """How long a submitted post may take to reach a terminal status."""
    return _float_env("PUBLISHHUB_IT_JOB_TIMEOUT", DEFAULT_JOB_TIMEOUT_SECONDS)


@pytest.fixture(scope="session")
def stack() -> Iterator[ComposeStack]:
    """
    A running PublishHub stack: Redis, the API, and the worker.

    Session-scoped because bringing the stack up is the expensive part of this
    suite — seconds when the dependency caches are warm, minutes on a clean
    checkout — and every test in it wants the same stack.
    """
    reason = docker_unavailable_reason()
    if reason is not None:
        # Skipping from the fixture rather than at import time means the reason is
        # reported once per test that needed the stack, and the suite is still
        # collectable — `pytest --collect-only` works on a machine without Docker.
        pytest.skip(reason)

    stack = ComposeStack(
        api_base_url=os.environ.get("PUBLISHHUB_API_URL", DEFAULT_API_BASE_URL).rstrip("/"),
        startup_timeout_seconds=_float_env(
            "PUBLISHHUB_IT_STARTUP_TIMEOUT", DEFAULT_STARTUP_TIMEOUT_SECONDS
        ),
    )
    stack.ensure_up()

    yield stack

    if not stack.was_preexisting:
        stack.down()


@pytest.fixture
def failing_worker(stack: ComposeStack) -> Iterator[ComposeStack]:
    """
    The same stack with a worker that fails every simulated publish.

    Replacing the container is the only way to change these settings — the worker
    validates its environment once, at startup — and it is also the honest way to
    exercise the path: the retry counter lives in the message envelope, so a
    worker recreated mid-suite picks jobs up exactly as a restarted pod would.

    The worker is restored afterwards only when the stack outlives the suite, and
    it is restored to the settings the adopted container actually had rather than
    to this suite's baseline — borrowing a developer's worker should not silently
    re-tune their simulated latency. When the suite started the stack, the
    teardown that follows removes the container anyway, so a recreate on the way
    out would cost a container start to change nothing.
    """
    stack.recreate_worker(FAILING_WORKER_ENV)
    try:
        yield stack
    finally:
        if stack.was_preexisting:
            stack.recreate_worker(stack.adopted_worker_env or BASELINE_WORKER_ENV)
