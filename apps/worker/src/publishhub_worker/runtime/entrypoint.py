"""
Process entrypoint (Requirements 3.6, 3.7, 5.5, 9.6).

Where the worker reads `os.environ`, opens a socket, and installs a signal handler.
The one exception is `observability/bootstrap.py`, which reads four variables of its
own because it has to run before this module's body does — see below. Everything
under this file is injectable and unit-tested; this file is the wiring, so it is kept
as thin as composing eight modules allows:

    tracer bootstrap     the first import, so `ddtrace` can still patch (14.2, 14.6)
    load_config          fail fast, naming the offending key (5.5)
    create_observability metrics client and tracing seam, from the validated switch
    create_logger        service, env, and the active span's ids on every line (14.3)
    ShutdownFlag         installed *before* the first message can be claimed (3.6)
    queue client         from the factory, so the backend is a variable, not a branch
    Redis for posts      a separate connection from the queue's, see `_create_redis`
    wait_for_queue       retry with backoff, report unready, never crash-loop (3.7)
    JobLoop.run          claim, publish, record, ack — until the flag says stop
    shutdown             mark unready, close connections, exit 0 (3.6, 9.6)

Order matters twice, and both are load-bearing:

- Configuration is validated before there is a logger, because the logger's
  `service` and `env` fields come from configuration. A `ConfigError` therefore
  reports on stderr and exits 1, with no traceback: the message names the variable
  to fix, and a stack trace above it would only bury the one useful line.
- The signal handler is installed before the queue client is built, and long before
  `wait_for_queue` can start sleeping. A worker that can consume before it can be
  told to stop is a worker that can lose an in-flight job, which is why tasks 4.2
  and 4.3 left this file for task 4.4 rather than shipping a loop without a handler.

`run_worker` returns an exit code instead of calling `sys.exit`, and every
dependency it builds can be substituted, so the whole startup-to-shutdown sequence
is exercised in `tests/test_runtime.py` against in-memory fakes: no Redis, no AWS,
no network, no sleeping, and no signals sent to the test process.

Observability lands here too, and its placement is load-bearing: the `ddtrace`
bootstrap is the first import below, because a tracer can only patch a module that
has not been evaluated yet. With `OBSERVABILITY_ENABLED` off that import reads four
environment variables and loads nothing (Requirement 14.6).
"""

from __future__ import annotations

# isort: off
# KEEP FIRST, and out of the sorted block below: importing this module initializes
# the tracer as a side effect, which has to happen before `redis` or `botocore` is
# evaluated anywhere in the process. An import-order "cleanup" here changes behavior.
from ..observability.bootstrap import tracing as process_tracing

# isort: on

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, TextIO

from ..config import ConfigError, WorkerConfig, load_config
from ..logging import LoggerDeps, WorkerLogger, create_logger
from ..observability import WorkerObservability, create_observability
from ..posts import PostStore, PostStoreCommands, RedisPostStore
from ..processing import JobLoop, JobLoopDeps, Publisher, utc_now
from ..queue import Env, QueueClient, QueueConfig, create_queue_client_from_config
from ..resilience import ConnectDeps, ReadinessFile, wait_for_queue
from .shutdown import (
    Closeable,
    ShutdownFlag,
    SignalDeps,
    close_resources,
    install_shutdown_handlers,
    worst_case_shutdown_seconds,
)

#: A clean stop: the flag was set, the in-flight job finished, connections closed.
EXIT_OK = 0
#: Bad configuration, an unreachable queue a caller asked us to give up on, or an
#: exception that escaped the loop. Non-zero so Kubernetes records a failure and
#: restarts rather than treating the exit as intentional.
EXIT_FAILURE = 1


class ClosableRedis(PostStoreCommands, Protocol):
    """
    The post store's connection: the one command it issues, plus `close`, because
    shutdown has to release it (Requirement 3.6).
    """

    def close(self) -> None: ...


@dataclass(frozen=True, kw_only=True, slots=True)
class RuntimeDeps:
    """
    Every seam the entrypoint has. Production passes none of them.

    They exist so that the composition itself — not a re-implementation of it — is
    what the tests drive. A test supplies a fake Redis, its own `ShutdownFlag`, and
    a publisher that flips that flag mid-job, then asserts the job was still
    completed and acked.
    """

    #: Environment for `load_config`. `None` means `os.environ`.
    env: Env | None = None
    #: Where log lines go, and at what level. `None` means stdout. A `LoggerDeps`
    #: that leaves `trace_context` unset has it filled in from the active tracer, so
    #: a test that only wants to capture the stream still gets log correlation.
    logger_deps: LoggerDeps | None = None
    #: Metrics and tracing. `None` means build them from configuration, using the
    #: tracer the bootstrap import already initialized (Requirement 14.6).
    observability: WorkerObservability | None = None
    #: The stop flag. `None` means build one bound to the logger.
    shutdown: ShutdownFlag | None = None
    #: Register OS signal handlers. Off in tests, which call the handler directly.
    install_signals: bool = True
    signal_deps: SignalDeps | None = None
    #: Builds the queue client for the resolved backend.
    create_queue: Callable[[QueueConfig], QueueClient] | None = None
    #: Builds the Redis connection the post store writes through.
    create_redis: Callable[[str], ClosableRedis] | None = None
    #: Overrides the post store, in which case no Redis connection is created.
    post_store: PostStore | None = None
    #: `None` means the default marker path, which the chart's probe reads.
    readiness: ReadinessFile | None = None
    #: `None` means the simulated publisher built from `config.simulation`.
    publisher: Publisher | None = None
    #: How the retry path and the startup wait sleep. Injected so no test waits.
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = utc_now
    #: `None` means retry the startup connection until it works or the flag fires.
    connect_max_attempts: int | None = None
    #: Bounds the number of receives. Production leaves it `None` and stops on the
    #: flag; a test sets it so a broken stop condition fails instead of hanging.
    max_iterations: int | None = None
    #: Where a configuration failure is reported. `None` means `sys.stderr`.
    stderr: TextIO | None = None


def _create_redis(redis_url: str) -> ClosableRedis:
    """
    Redis for the post records — a *second* connection, even when the queue backend
    is also Redis.

    A blocking `BRPOPLPUSH` occupies its connection for the whole
    `POLL_WAIT_SECONDS` window, so sharing one with the post store would make every
    status write wait out a poll. Same reasoning as `createRedis` in
    `apps/api/src/index.ts`.

    Imported here rather than at module scope so this module imports in an
    environment without redis-py, and so an SQS-backed worker still needs it only
    for post records.
    """
    import redis

    return redis.Redis.from_url(redis_url, decode_responses=True)


def run_worker(config: WorkerConfig, deps: RuntimeDeps | None = None) -> int:
    """
    Start the worker and run until it is told to stop. Returns the process exit
    code; never raises for an expected failure.
    """
    dependencies = RuntimeDeps() if deps is None else deps

    observability = (
        create_observability(config, tracing=process_tracing, env=dependencies.env)
        if dependencies.observability is None
        else dependencies.observability
    )

    # With tracing active every log line gains the ids of the span that produced it,
    # which is what makes a log and a trace one click apart (Requirement 14.3). With
    # it inert the provider returns None and the fields never appear, so the output
    # is byte-identical to what it was before observability existed.
    logger = create_logger(config, _with_trace_context(dependencies, observability))
    flag = ShutdownFlag(logger=logger) if dependencies.shutdown is None else dependencies.shutdown

    # Before the queue client exists, and so before anything can be claimed.
    if dependencies.install_signals:
        install_shutdown_handlers(flag, dependencies.signal_deps)

    readiness = ReadinessFile() if dependencies.readiness is None else dependencies.readiness
    budget_seconds = worst_case_shutdown_seconds(config)

    logger.info(
        "worker starting",
        backend=config.queue.backend,
        poll_wait_seconds=config.poll_wait_seconds,
        max_attempts=config.max_attempts,
        # The number the chart's `terminationGracePeriodSeconds` has to exceed
        # (Requirement 9.6). Logged rather than commented so an operator comparing
        # the two does not have to compute one of them.
        shutdown_budget_seconds=budget_seconds,
        readiness_file=str(readiness.path),
        signal_handlers_installed=dependencies.install_signals,
        # Logged because "why is there no telemetry" is a question with two answers,
        # and this line rules one of them out.
        observability_enabled=observability.enabled,
        tracing_enabled=observability.tracing.enabled,
    )

    # Closed in the order they are appended, which is construction order — queue
    # first, then Redis. Registered as each one is built rather than afterwards, so
    # a constructor that fails cannot leave an earlier connection open.
    resources: list[Closeable] = []
    exit_code = EXIT_OK

    try:
        create_queue = (
            create_queue_client_from_config
            if dependencies.create_queue is None
            else dependencies.create_queue
        )
        queue = create_queue(config.queue)
        resources.append(Closeable("queue", queue.close))

        if dependencies.post_store is not None:
            post_store: PostStore = dependencies.post_store
        else:
            create_redis = (
                _create_redis if dependencies.create_redis is None else dependencies.create_redis
            )
            connection = create_redis(config.redis_url)
            resources.append(Closeable("redis", connection.close))
            post_store = RedisPostStore(connection)

        exit_code = _consume(
            config=config,
            deps=dependencies,
            queue=queue,
            post_store=post_store,
            logger=logger,
            flag=flag,
            readiness=readiness,
            observability=observability,
        )
    # Deliberately broad, and deliberately not re-raised. An exception from Redis
    # or SQS itself escapes the loop by design (see `processing/job_loop.py`), and
    # what it needs here is the same orderly close as a clean stop plus a non-zero
    # exit so the restart is recorded as a failure — not a traceback on stderr and
    # two connections left to the kernel.
    except Exception as error:
        logger.error("worker failed", exc=error, error_type=type(error).__name__)
        exit_code = EXIT_FAILURE
    finally:
        # Unready first: a worker that is closing its queue connection must stop
        # reporting itself as able to consume, and this runs on the failure path
        # too (Requirement 3.7).
        readiness.mark_unready()
        close_resources(resources, logger)
        logger.info(
            "worker stopped",
            reason=flag.reason,
            signals_received=flag.signals_received,
            exit_code=exit_code,
        )

    return exit_code


def _with_trace_context(
    deps: RuntimeDeps,
    observability: WorkerObservability,
) -> LoggerDeps:
    """
    The caller's logger seams, with the tracer's log-correlation provider filled in.

    Wired here rather than inside `create_logger` because the logging module knows
    nothing about tracing on purpose (see its docstring) — the provider is a callable
    it invokes, and this is the one place that has both halves in hand.
    """
    if deps.logger_deps is None:
        return LoggerDeps(trace_context=observability.tracing.trace_context)
    if deps.logger_deps.trace_context is not None:
        # An explicit provider wins: a test that supplies one is testing it.
        return deps.logger_deps
    return replace(deps.logger_deps, trace_context=observability.tracing.trace_context)


def _consume(
    *,
    config: WorkerConfig,
    deps: RuntimeDeps,
    queue: QueueClient,
    post_store: PostStore,
    logger: WorkerLogger,
    flag: ShutdownFlag,
    readiness: ReadinessFile,
    observability: WorkerObservability,
) -> int:
    """
    Wait for the queue to answer, then consume until the flag says stop.

    Split out so that `run_worker` reads as construct-run-shutdown, with one `try`
    covering construction and consumption alike.
    """
    connection = wait_for_queue(
        queue=queue,
        logger=logger,
        readiness=readiness,
        deps=ConnectDeps(
            sleep=deps.sleep,
            # The second half of the stop-flag wiring. A pod deleted while the
            # queue is still unreachable exits here instead of sitting out a
            # backoff it will never finish (Requirements 3.6, 3.7).
            should_continue=flag.should_continue,
            max_attempts=deps.connect_max_attempts,
        ),
    )

    if not connection.connected:
        # Two ways to get here. Told to stop before the queue answered: nothing was
        # claimed, nothing was lost, and a pod deleted during a dependency outage
        # is not a failure — exit 0. Or a caller asked for a bounded wait and it
        # ran out, which is a real startup failure.
        return EXIT_OK if flag.stopping else EXIT_FAILURE

    loop = JobLoop(
        config=config,
        queue=queue,
        post_store=post_store,
        logger=logger,
        deps=JobLoopDeps(
            publisher=deps.publisher,
            now=deps.now,
            # The seam this whole task exists to connect. Checked before each
            # receive and never mid-job, so a signal that arrives while a job is
            # in flight lets that job finish and be acked (Requirement 3.6).
            should_continue=flag.should_continue,
            sleep=deps.sleep,
            observability=observability,
        ),
    )
    loop.run(max_iterations=deps.max_iterations)
    return EXIT_OK


def main(deps: RuntimeDeps | None = None) -> int:
    """
    Load configuration, then run. Returns the exit code for `__main__.py` to hand
    to `SystemExit`.
    """
    dependencies = RuntimeDeps() if deps is None else deps

    try:
        config = load_config(dependencies.env)
    except ConfigError as error:
        # No logger yet: its `service` and `env` fields come from the configuration
        # that just failed to load. stderr is the only honest channel, and the
        # message names the offending variable (Requirement 5.5). No traceback —
        # this is a configuration mistake, not a crash.
        stream = sys.stderr if dependencies.stderr is None else dependencies.stderr
        stream.write(f"worker startup failed: {error}\n")
        return EXIT_FAILURE

    return run_worker(config, dependencies)
