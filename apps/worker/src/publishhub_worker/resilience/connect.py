"""
Startup connection resilience (Requirement 3.7).

A worker that starts while its queue is unreachable must not die. Both backends
build their client lazily — `redis.Redis.from_url` opens no socket, and a `boto3`
client resolves nothing until the first call — so the failure surfaces on the
first real command, which without this module would be the job loop's `receive`
and would take the process with it. Kubernetes would restart it, the queue would
still be down, and the pod would sit in `CrashLoopBackOff` with an ever-growing
restart count that says nothing about the actual fault.

So the worker probes the queue first, retries with exponential backoff, and
reports unready while it waits. `depth()` is the probe: it is the cheapest command
either backend offers that proves the connection works end to end (`LLEN` on
Redis, `GetQueueAttributes` on SQS), and it is the same number KEDA scales on, so
a successful probe also confirms the value the autoscaler will read.

Retrying is unbounded by default, which is the point rather than an oversight: a
process that gives up and exits is a process that crash-loops. Staying alive and
unready keeps one pod visible with a readiness failure a human can read, and lets
the worker start consuming the moment the dependency returns. `max_attempts` is
available for tests and for a caller that wants a bounded wait.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..logging import WorkerLogger
from ..queue import QueueClient
from .backoff import DEFAULT_CONNECT_BACKOFF, BackoffPolicy
from .readiness import ReadinessFile


def always_continue() -> bool:
    """Default stop condition: keep going. Task 4.4 supplies a `SIGTERM` flag."""
    return True


@dataclass(frozen=True, kw_only=True, slots=True)
class ConnectDeps:
    """Construction seams, so the retry schedule is asserted without sleeping."""

    sleep: Callable[[float], None] = time.sleep
    backoff: BackoffPolicy = DEFAULT_CONNECT_BACKOFF
    #: Checked before every attempt, so a `SIGTERM` during a backoff wait ends the
    #: wait instead of outliving the grace period (spec task 4.4).
    should_continue: Callable[[], bool] = always_continue
    #: `None` means retry until the queue answers or the stop condition fires.
    max_attempts: int | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class ConnectResult:
    """What the wait did. Returned so a caller can log it and a test can assert it."""

    connected: bool
    #: Probes issued, including the successful one.
    attempts: int
    #: Total time spent in backoff waits.
    waited_seconds: float
    #: Queue depth reported by the successful probe, `None` when it never succeeded.
    depth: int | None = None
    #: The last failure, for a caller that wants to report why it gave up.
    error: BaseException | None = None


def wait_for_queue(
    *,
    queue: QueueClient,
    logger: WorkerLogger,
    readiness: ReadinessFile | None = None,
    deps: ConnectDeps | None = None,
) -> ConnectResult:
    """
    Probe the queue until it answers, backing off between attempts.

    Marks the readiness file unready on entry and ready on success, so the window
    where the worker is up but cannot consume is visible to a probe rather than
    only in the logs.
    """
    dependencies = ConnectDeps() if deps is None else deps

    if readiness is not None:
        # Unready first, unconditionally: a restarted container can inherit a
        # marker file from an `emptyDir` that outlived the previous process.
        readiness.mark_unready()

    attempts = 0
    waited = 0.0
    last_error: BaseException | None = None

    while dependencies.should_continue():
        attempts += 1
        try:
            depth = queue.depth()
        # Deliberately broad: redis-py, botocore, and a DNS failure raise
        # different types, and all of them mean the same thing here.
        except Exception as error:
            last_error = error
            exhausted = (
                dependencies.max_attempts is not None and attempts >= dependencies.max_attempts
            )
            if exhausted:
                logger.error(
                    "queue unreachable, giving up",
                    exc=error,
                    attempts=attempts,
                    waited_seconds=round(waited, 3),
                )
                return ConnectResult(
                    connected=False,
                    attempts=attempts,
                    waited_seconds=waited,
                    error=error,
                )

            delay = dependencies.backoff.delay_for(attempts)
            # Warning, not error: an unreachable dependency at startup is an
            # expected state during a rollout, and the retry is the handling.
            # The error type and message are named so the log line distinguishes
            # "Redis is not up yet" from "the URL is wrong".
            logger.warning(
                "queue unreachable, retrying",
                attempt=attempts,
                retry_in_seconds=delay,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            dependencies.sleep(delay)
            waited += delay
            continue

        if readiness is not None:
            readiness.mark_ready()

        logger.info(
            "queue reachable",
            attempts=attempts,
            waited_seconds=round(waited, 3),
            depth=depth,
        )
        return ConnectResult(
            connected=True,
            attempts=attempts,
            waited_seconds=waited,
            depth=depth,
        )

    # The stop condition fired, either before the first probe or during a wait.
    logger.info(
        "queue connection abandoned before the queue became reachable",
        attempts=attempts,
        waited_seconds=round(waited, 3),
    )
    return ConnectResult(
        connected=False,
        attempts=attempts,
        waited_seconds=waited,
        error=last_error,
    )
