"""
Graceful shutdown (Requirements 3.6, 9.6).

Scale-to-zero is only safe if a worker being scaled away finishes what it already
claimed. Kubernetes sends `SIGTERM` and starts a stopwatch worth
`terminationGracePeriodSeconds`; whatever is still running when it expires is
`SIGKILL`ed. So the handler here does the least a handler can do — it sets a flag —
and the loop reads that flag at the one moment where stopping is free: *before* the
next receive, never in the middle of a job.

    signal arrives  ->  flag set  ->  in-flight job finishes and is acked
                                  ->  loop exits before claiming another
                                  ->  readiness marker removed
                                  ->  queue and Redis closed
                                  ->  exit 0

That ordering is Requirement 3.6 in full: finish the in-flight job, claim no new
ones, close connections, exit inside the grace period. Nothing here cancels work
in progress, because cancelling is what loses a message.

## Why a flag and not an exception

Raising from the signal handler would interrupt the blocking receive immediately,
which is attractive — an idle worker would exit in milliseconds rather than at the
end of its poll window — but it can also land in the middle of publishing, between
the post-record write and the ack. The message would survive (it stays in the
Redis processing list until the reaper returns it, and an unacked SQS message
becomes visible again), yet the job would be published twice for no reason. A flag
costs at most one `POLL_WAIT_SECONDS` of extra shutdown time, which the grace
period budget accommodates by design, and it makes the shutdown path have exactly
one shape rather than one per interruption point.

## A second signal

Kubernetes sends one `SIGTERM` and then `SIGKILL`; a developer holding Ctrl-C
sends as many `SIGINT`s as they like. The first one starts the sequence. A second
one, arriving while the first is still being honored, cannot usefully restart it —
so instead of ignoring it and looking wedged, the installer restores the default
disposition for every signal it manages. The next signal after that terminates the
process the way it would have if this module had never run.

That escalation is deliberately not an immediate exit: at-least-once delivery
means the in-flight message is recoverable either way, so the choice is between
"finish and ack" and "be killed and rely on the reaper", and the only reason to
prefer the second is an operator who has explicitly asked twice.

## The grace period budget (Requirement 9.6)

`worst_case_shutdown_seconds` computes, from validated configuration, the longest
the sequence above can take. The chart's `terminationGracePeriodSeconds` (spec task
9.2) has to exceed it, and the worker logs the number at startup so the two can be
compared without reading either file.
"""

from __future__ import annotations

import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import FrameType
from typing import Any

from ..config import WorkerConfig
from ..logging import WorkerLogger
from ..queue import PLATFORM_ALLOW_LIST
from ..resilience import DEFAULT_RETRY_BACKOFF, BackoffPolicy

#: Signals that mean "stop": Kubernetes sends the first, a terminal the second.
SHUTDOWN_SIGNALS: tuple[int, ...] = (signal.SIGTERM, signal.SIGINT)

#: Slack in the grace-period budget for removing the readiness marker and closing
#: two connections. Generous on purpose: it is the term that absorbs a slow `QUIT`
#: to a struggling Redis, and it is also what covers a signal that lands in the
#: moment before a blocking receive rather than during one.
CLOSING_ALLOWANCE_SECONDS = 5.0

#: What a handler must accept — the C-level signature CPython calls it with.
SignalHandler = Callable[[int, FrameType | None], None]


def signal_name(signum: int) -> str:
    """`SIGTERM` rather than `15`, for the log line."""
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


class ShutdownFlag:
    """
    The stop flag, and the only mutable state graceful shutdown needs.

    `should_continue` is passed straight to `JobLoopDeps.should_continue` and
    `ConnectDeps.should_continue`, which is what makes one flag cover both places
    the worker can be when a signal arrives: mid-job in the loop, or sitting out a
    backoff because the queue is not up yet. Without the second wiring, a pod
    deleted during a Redis outage would keep waiting until the grace period ran
    out and then be killed.

    Flipping is one-way. A worker that has been told to stop does not un-stop, so
    there is no `resume`.
    """

    __slots__ = ("_logger", "_reason", "_signals_received")

    def __init__(self, *, logger: WorkerLogger | None = None) -> None:
        # Optional so a caller with no logger yet — or a test — can still hold a
        # flag. When present, one line is written per signal, because a job that
        # runs for a minute after `SIGTERM` otherwise looks like a hang.
        #
        # Logging from a signal handler is safe here specifically: handlers run in
        # the main thread, and `logging` guards its handlers with an `RLock`, which
        # the same thread can re-acquire. It would not be safe if the handler ran
        # in a thread or after a fork, and neither happens in this process.
        self._logger = logger
        self._reason: str | None = None
        self._signals_received = 0

    @property
    def stopping(self) -> bool:
        """Whether a stop has been requested."""
        return self._reason is not None

    @property
    def reason(self) -> str | None:
        """What asked for the stop — a signal name, or a caller-supplied reason."""
        return self._reason

    @property
    def signals_received(self) -> int:
        """How many signals have arrived. Above one means someone is impatient."""
        return self._signals_received

    def should_continue(self) -> bool:
        """The predicate the loop and the startup wait check. Never raises."""
        return self._reason is None

    def handle(self, signum: int, frame: FrameType | None = None) -> None:
        """
        The signal handler. Takes CPython's `(signum, frame)` signature so it can
        be registered directly, and is a plain method so tests drive shutdown by
        calling it instead of signalling the test runner.
        """
        self._signals_received += 1
        name = signal_name(signum)

        if self._reason is None:
            self._reason = name
            if self._logger is not None:
                self._logger.info(
                    "shutdown requested, finishing in-flight work",
                    signal=name,
                    signals_received=self._signals_received,
                )
            return

        if self._logger is not None:
            # Warning, not info: the operator is telling us the graceful path is
            # taking too long, which is worth seeing next to the grace period.
            self._logger.warning(
                "shutdown already in progress",
                signal=name,
                signals_received=self._signals_received,
                first_signal=self._reason,
            )

    def request_stop(self, reason: str) -> None:
        """
        Stop for a reason that is not a signal — a fatal error in the wiring, or a
        test. Idempotent: the first reason is the one that is kept, because it is
        the one that explains the shutdown.
        """
        if self._reason is not None:
            return
        self._reason = reason
        if self._logger is not None:
            self._logger.info("shutdown requested, finishing in-flight work", reason=reason)


@dataclass(frozen=True, kw_only=True, slots=True)
class SignalDeps:
    """
    Registration seams, so no test ever sends a real signal to the test process.

    `register` has `signal.signal`'s shape: it takes a signal number and a handler
    (or one of the `SIG_DFL` / `SIG_IGN` constants) and returns the previous one.
    """

    signals: tuple[int, ...] = SHUTDOWN_SIGNALS
    register: Callable[[int, Any], Any] = signal.signal


def install_shutdown_handlers(
    flag: ShutdownFlag,
    deps: SignalDeps | None = None,
) -> dict[int, SignalHandler]:
    """
    Point every shutdown signal at `flag`, and return the handlers that were
    installed, keyed by signal number.

    Returning them is what lets a test assert the registration and then invoke
    exactly what the kernel would have invoked. `signal.signal` may only be called
    from the main thread, which is where the entrypoint runs.
    """
    dependencies = SignalDeps() if deps is None else deps

    def handler(signum: int, frame: FrameType | None = None) -> None:
        flag.handle(signum, frame)
        if flag.signals_received > 1:
            # Hand every managed signal back to the OS default. The sequence
            # already under way keeps running; one more signal now ends the
            # process instead of being absorbed by a handler that has nothing
            # left to do with it.
            for managed in dependencies.signals:
                dependencies.register(managed, signal.SIG_DFL)

    installed: dict[int, SignalHandler] = {}
    for signum in dependencies.signals:
        dependencies.register(signum, handler)
        installed[signum] = handler
    return installed


@dataclass(frozen=True, slots=True)
class Closeable:
    """A connection to close on the way out. `name` is what the log line says."""

    name: str
    close: Callable[[], None]


def close_resources(resources: Sequence[Closeable], logger: WorkerLogger) -> None:
    """
    Close everything, in the order given, and keep going when one fails.

    The process is exiting either way, and stopping at the first failed `close`
    would leave the remaining connections open — the opposite of the point.
    """
    for resource in resources:
        try:
            resource.close()
        # Deliberately broad: redis-py, botocore, and a socket error raise
        # different types, and none of them changes what happens next.
        except Exception as error:
            logger.error(
                "failed to close dependency during shutdown",
                exc=error,
                resource=resource.name,
            )
        else:
            logger.info("dependency closed", resource=resource.name)


def worst_case_shutdown_seconds(
    config: WorkerConfig,
    *,
    retry_backoff: BackoffPolicy = DEFAULT_RETRY_BACKOFF,
) -> float:
    """
    Longest the shutdown sequence can take once a signal has arrived, in seconds,
    from validated configuration (Requirement 9.6).

    Three terms, and nothing else runs after the flag is set:

    1. The in-flight job's simulated publishes. Worst case is every platform in
       the allow-list at the configured latency, because a job may target all of
       them.
    2. One backoff wait, if that job's publish failed and the loop is retrying.
       Only one — the retry re-enqueues and the loop then sees the flag.
    3. Closing the queue client and the Redis connection, which are local socket
       teardowns.

    `POLL_WAIT_SECONDS` is deliberately absent. A signal arriving mid-poll is
    absorbed by CPython and the receive returns, so an idle worker exits promptly;
    the poll window only delays shutdown if the signal lands in the microseconds
    before the call, which the closing allowance covers.

    One case this number does *not* cover, because it is not the steady state:
    a signal arriving while the startup wait is sitting out a backoff. Since PEP
    475, `time.sleep` resumes for the remainder of its interval after a handler
    runs, so the flag is not observed until that sleep ends — up to
    `DEFAULT_CONNECT_BACKOFF.max_seconds`. A worker in that state has claimed
    nothing, so nothing can be lost; it only has to exit before the grace period,
    which is why the connect backoff's ceiling is 30s rather than unbounded.

    The chart's `terminationGracePeriodSeconds` (spec task 9.2) must exceed this,
    and the worker logs it at startup so the comparison needs no arithmetic.
    """
    publishing = len(PLATFORM_ALLOW_LIST) * config.simulation.latency_ms / 1000
    # The largest single delay the loop can wait: the last retry before attempts
    # run out. `max_attempts` of 1 never retries, so there is no wait at all.
    retrying = (
        0.0 if config.max_attempts <= 1 else retry_backoff.delay_for(config.max_attempts - 1)
    )
    return round(publishing + retrying + CLOSING_ALLOWANCE_SECONDS, 3)
