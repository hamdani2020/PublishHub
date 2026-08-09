"""
Graceful shutdown and process entrypoint tests (Requirements 3.6, 9.6).

The behavior these tests exist for is one sentence: a job the worker had already
claimed when `SIGTERM` arrived is published, recorded, and acked before the process
exits, and no further job is claimed. Everything else here is in service of that —
the stop flag reaching both the job loop and the startup connection wait, the exit
codes Kubernetes reads, the readiness marker disappearing before the connections
close, and a configuration mistake exiting with one readable line instead of a
traceback.

Three constraints shape how they are written:

- **No signals are sent to the test process.** `pytest` is the process, and a real
  `SIGTERM` would end the run. The handler is a plain method, so shutdown is driven
  by calling it with the signal number the kernel would have passed — including
  from inside a publisher, which is how "the signal arrived mid-job" is expressed.
- **No Redis, no AWS, no network.** The entrypoint's Redis and queue constructors
  are seams; these tests pass the in-memory fake, so the real `RedisQueueClient`,
  `RedisPostStore`, and `JobLoop` are exercised rather than mocked.
- **Nothing sleeps.** Every wait goes through the injected `sleep`, which records
  instead of waiting.

`max_iterations` is set on every run that reaches the loop. It is not what stops
the worker — the flag is — but a bug in the flag wiring would otherwise hang the
suite forever instead of failing it.
"""

from __future__ import annotations

import io
import json
import signal
from pathlib import Path
from typing import Any

import pytest

from publishhub_worker.config import WorkerConfig, load_config
from publishhub_worker.logging import LoggerDeps, WorkerLogger, create_logger
from publishhub_worker.posts import PlatformResult, RedisPostStore
from publishhub_worker.queue import (
    DEFAULT_REDIS_QUEUE_KEYS,
    PublishJob,
    QueueConfig,
    ReceivedJob,
    RedisQueueClient,
    create_publish_job,
)
from publishhub_worker.queue.testing import FakeRedis
from publishhub_worker.resilience import ReadinessFile
from publishhub_worker.runtime import (
    EXIT_FAILURE,
    EXIT_OK,
    SHUTDOWN_SIGNALS,
    Closeable,
    RuntimeDeps,
    ShutdownFlag,
    SignalDeps,
    close_resources,
    install_shutdown_handlers,
    main,
    run_worker,
    signal_name,
    worst_case_shutdown_seconds,
)

KEYS = DEFAULT_REDIS_QUEUE_KEYS
POST_ID = "post_01HZX3QK7M9V4TDR8N2C5EAB6F"
RECORD_KEY = f"publishhub:post:{POST_ID}"

#: A configuration the worker accepts, with the simulation turned instant and
#: deterministic so a test asserts wiring rather than a random draw.
BASE_ENV: dict[str, str] = {
    "QUEUE_BACKEND": "redis",
    "REDIS_URL": "redis://publishhub-redis:6379",
    "POLL_WAIT_SECONDS": "1",
    "SIMULATE_LATENCY_MS": "0",
    "SIMULATE_FAILURE_RATE": "0",
    "DD_ENV": "development",
}

#: Bound on every run that reaches the loop, so a broken stop flag fails the test
#: instead of hanging it. The flag is what actually ends each run below.
LOOP_BOUND = 5


class Sink:
    """Captures what the run logged, as parsed JSON objects."""

    def __init__(self, stream: io.StringIO) -> None:
        self._stream = stream

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self._stream.getvalue().splitlines() if line]

    def events(self) -> list[str]:
        return [line["msg"] for line in self.lines()]

    def all(self, event: str) -> list[dict[str, Any]]:
        return [line for line in self.lines() if line["msg"] == event]

    def find(self, event: str) -> dict[str, Any]:
        matches = self.all(event)
        assert len(matches) == 1, f'expected one "{event}" line, got {len(matches)}'
        return matches[0]


class Recorder:
    """
    Signal registration seam. Records every call `signal.signal` would have
    received, and keeps the installed handler so a test can invoke exactly what the
    kernel would have invoked.
    """

    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[tuple[int, Any]] = []
        self._events = events

    def __call__(self, signum: int, handler: Any) -> Any:
        self.calls.append((signum, handler))
        if self._events is not None:
            self._events.append(f"register:{signal_name(signum)}")
        return signal.SIG_DFL

    @property
    def registered(self) -> list[int]:
        return [signum for signum, _ in self.calls]

    def handler_for(self, signum: int) -> Any:
        handlers = [handler for registered, handler in self.calls if registered == signum]
        assert handlers, f"no handler registered for {signal_name(signum)}"
        return handlers[-1]

    @property
    def defaulted(self) -> list[int]:
        """Signals handed back to the OS default disposition."""
        return [signum for signum, handler in self.calls if handler is signal.SIG_DFL]


class Runtime:
    """A wired entrypoint plus the fakes behind it."""

    _counter = 0

    def __init__(self, tmp_path: Path, *, env: dict[str, str] | None = None) -> None:
        Runtime._counter += 1
        self.env = {**BASE_ENV, **(env or {})}
        self.queue_redis = FakeRedis()
        #: A second connection, as in production: a blocking receive occupies its
        #: own, so the post store never waits out a poll window.
        self.post_redis = FakeRedis()
        self.stream = io.StringIO()
        self.sink = Sink(self.stream)
        self.readiness = ReadinessFile(tmp_path / "worker.ready")
        self.flag = ShutdownFlag()
        self.sleeps: list[float] = []
        #: Ordered trace of the wiring steps a test wants to see in sequence.
        self.events: list[str] = []
        self.signals = Recorder(self.events)
        self._name = f"test-runtime-{Runtime._counter}"
        self._queue: RedisQueueClient | None = None

    # --- seams ----------------------------------------------------------------

    def create_queue(self, _config: QueueConfig) -> RedisQueueClient:
        self.events.append("create_queue")
        self._queue = RedisQueueClient(self.queue_redis)
        return self._queue

    def create_redis(self, _url: str) -> FakeRedis:
        self.events.append("create_redis")
        return self.post_redis

    def deps(self, **overrides: Any) -> RuntimeDeps:
        arguments: dict[str, Any] = {
            "env": self.env,
            "logger_deps": LoggerDeps(stream=self.stream, name=self._name),
            "shutdown": self.flag,
            "signal_deps": SignalDeps(register=self.signals),
            "create_queue": self.create_queue,
            "create_redis": self.create_redis,
            "readiness": self.readiness,
            "sleep": self.sleeps.append,
            "max_iterations": LOOP_BOUND,
        }
        arguments.update(overrides)
        return RuntimeDeps(**arguments)

    def config(self) -> WorkerConfig:
        return load_config(self.env)

    # --- assertions -----------------------------------------------------------

    def enqueue(self, job: PublishJob) -> None:
        RedisQueueClient(self.queue_redis).enqueue(job)

    def record(self) -> dict[str, str]:
        return self.post_redis.fields(RECORD_KEY)

    def queued(self) -> list[str]:
        return self.queue_redis.contents(KEYS.jobs)

    def processing(self) -> list[str]:
        return self.queue_redis.contents(KEYS.processing)

    def receives(self) -> int:
        return sum(
            1
            for call in self.queue_redis.calls
            if call[0] in {"brpoplpush", "rpoplpush"} and call[1] == KEYS.jobs
        )


def make_logger(stream: io.StringIO, name: str) -> WorkerLogger:
    return create_logger(load_config(BASE_ENV), LoggerDeps(stream=stream, name=name))


def make_job(**overrides: Any) -> PublishJob:
    arguments: dict[str, Any] = {
        "post_id": POST_ID,
        "content": "hello world",
        "platforms": ("twitter", "linkedin"),
    }
    arguments.update(overrides)
    return create_publish_job(**arguments)


def publishes_everything(job: PublishJob) -> tuple[PlatformResult, ...]:
    return tuple(
        PlatformResult(platform=platform, status="published", duration_ms=11)
        for platform in job.platforms
    )


def signalled_while_publishing(runtime: Runtime, *, watch: list[bool] | None = None):
    """
    A publisher that delivers `SIGTERM` in the middle of the job, which is the
    moment Requirement 3.6 is about: the worker is holding a claimed message.
    """

    def publish(job: PublishJob) -> tuple[PlatformResult, ...]:
        if watch is not None:
            # Sampled mid-job, so "was it ready while working" is observed rather
            # than inferred from the end state.
            watch.append(runtime.readiness.ready)
        runtime.flag.handle(signal.SIGTERM)
        return publishes_everything(job)

    return publish


# --- the stop flag ------------------------------------------------------------


def test_the_flag_lets_work_continue_until_a_signal_arrives() -> None:
    flag = ShutdownFlag()

    assert flag.should_continue() is True
    assert flag.stopping is False
    assert flag.reason is None

    flag.handle(signal.SIGTERM)

    assert flag.should_continue() is False
    assert flag.stopping is True
    assert flag.reason == "SIGTERM"
    assert flag.signals_received == 1


def test_the_flag_keeps_the_first_reason_and_counts_the_rest() -> None:
    # The first signal is the one that explains the shutdown; a later one only says
    # someone is impatient.
    flag = ShutdownFlag()

    flag.handle(signal.SIGTERM)
    flag.handle(signal.SIGINT)
    flag.request_stop("fatal error")

    assert flag.reason == "SIGTERM"
    assert flag.signals_received == 2
    assert flag.should_continue() is False


def test_a_non_signal_caller_can_request_the_same_stop() -> None:
    flag = ShutdownFlag()

    flag.request_stop("configuration reloaded")

    assert flag.stopping is True
    assert flag.reason == "configuration reloaded"
    assert flag.signals_received == 0


def test_names_an_unknown_signal_number_without_raising() -> None:
    # `signal_name` feeds a log field, and a logger is not worth crashing over.
    assert signal_name(int(signal.SIGTERM)) == "SIGTERM"
    assert signal_name(9999) == "signal 9999"


# --- handler installation -----------------------------------------------------


def test_installs_a_handler_for_sigterm_and_for_ctrl_c() -> None:
    flag = ShutdownFlag()
    recorder = Recorder()

    installed = install_shutdown_handlers(flag, SignalDeps(register=recorder))

    assert recorder.registered == list(SHUTDOWN_SIGNALS)
    assert set(installed) == set(SHUTDOWN_SIGNALS)


def test_the_installed_handler_is_what_sets_the_flag() -> None:
    flag = ShutdownFlag()
    recorder = Recorder()
    install_shutdown_handlers(flag, SignalDeps(register=recorder))

    # Invoked with the arguments CPython passes a signal handler.
    recorder.handler_for(signal.SIGTERM)(int(signal.SIGTERM), None)

    assert flag.reason == "SIGTERM"


def test_a_second_signal_hands_every_signal_back_to_the_os_default() -> None:
    # The documented escalation: the sequence already under way keeps running, and
    # one more signal after this ends the process rather than being absorbed by a
    # handler with nothing left to do.
    flag = ShutdownFlag()
    recorder = Recorder()
    install_shutdown_handlers(flag, SignalDeps(register=recorder))
    handler = recorder.handler_for(signal.SIGTERM)

    handler(int(signal.SIGTERM), None)
    assert recorder.defaulted == []

    handler(int(signal.SIGTERM), None)

    assert recorder.defaulted == list(SHUTDOWN_SIGNALS)
    # Still stopping, not wedged, and still on the first reason.
    assert flag.stopping is True
    assert flag.signals_received == 2


# --- closing connections ------------------------------------------------------


def test_closes_every_connection_even_when_one_fails() -> None:
    runtime_stream = io.StringIO()
    logger = make_logger(runtime_stream, "test-runtime-close")
    closed: list[str] = []

    def failing() -> None:
        raise ConnectionError("connection already gone")

    close_resources(
        [
            Closeable("queue", failing),
            Closeable("redis", lambda: closed.append("redis")),
        ],
        logger,
    )

    # The process is exiting either way; stopping at the first failure would leave
    # the rest of the connections open.
    assert closed == ["redis"]
    sink = Sink(runtime_stream)
    assert sink.find("failed to close dependency during shutdown")["resource"] == "queue"
    assert sink.find("dependency closed")["resource"] == "redis"


# --- the grace period budget (Requirement 9.6) --------------------------------


def test_reports_the_worst_case_shutdown_time_the_grace_period_must_exceed() -> None:
    # Four platforms in the allow-list at 500ms, plus the last retry's 2s backoff,
    # plus the allowance for closing: the number the chart is sized against.
    config = load_config({**BASE_ENV, "SIMULATE_LATENCY_MS": "500", "MAX_ATTEMPTS": "3"})

    assert worst_case_shutdown_seconds(config) == pytest.approx(9.0)


def test_the_budget_drops_the_retry_term_when_retries_are_disabled() -> None:
    config = load_config({**BASE_ENV, "SIMULATE_LATENCY_MS": "500", "MAX_ATTEMPTS": "1"})

    assert worst_case_shutdown_seconds(config) == pytest.approx(7.0)


# --- the in-flight job (Requirement 3.6) --------------------------------------


def test_a_job_claimed_before_sigterm_is_published_recorded_and_acked(tmp_path: Path) -> None:
    # The property this task exists for: scale-down never loses a message.
    runtime = Runtime(tmp_path)
    runtime.enqueue(make_job())

    exit_code = run_worker(
        runtime.config(),
        runtime.deps(publisher=signalled_while_publishing(runtime), install_signals=False),
    )

    assert exit_code == EXIT_OK
    assert runtime.record()["status"] == "published"
    # Acked: gone from the jobs list and out of the processing list, so nothing is
    # left for the reaper to return.
    assert runtime.queued() == []
    assert runtime.processing() == []
    assert runtime.queue_redis.contents(KEYS.dead_letter) == []
    assert runtime.sink.find("job published")["post_id"] == POST_ID


def test_claims_no_further_job_once_the_signal_has_arrived(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path)
    runtime.enqueue(make_job(content="first"))
    runtime.enqueue(make_job(content="second"))

    exit_code = run_worker(
        runtime.config(),
        runtime.deps(publisher=signalled_while_publishing(runtime), install_signals=False),
    )

    assert exit_code == EXIT_OK
    # One receive, one job. The second envelope is still queued for whichever
    # replica is left, which is the half of Requirement 3.6 that says "avoid
    # claiming new jobs".
    assert runtime.receives() == 1
    assert len(runtime.queued()) == 1
    assert runtime.sink.find("job loop stopped")["iterations"] == 1


def test_reports_unready_and_closes_both_connections_before_exiting(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path)
    runtime.enqueue(make_job())
    ready_during_job: list[bool] = []

    exit_code = run_worker(
        runtime.config(),
        runtime.deps(
            publisher=signalled_while_publishing(runtime, watch=ready_during_job),
            install_signals=False,
        ),
    )

    assert exit_code == EXIT_OK
    # Ready while consuming, unready on the way out — so a probe never points at a
    # worker whose queue connection is being closed.
    assert ready_during_job == [True]
    assert runtime.readiness.ready is False
    assert runtime.queue_redis.closed is True
    assert runtime.post_redis.closed is True
    assert [line["resource"] for line in runtime.sink.all("dependency closed")] == [
        "queue",
        "redis",
    ]


def test_records_the_reason_and_the_exit_code_on_the_way_out(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path)
    runtime.enqueue(make_job())

    run_worker(
        runtime.config(),
        runtime.deps(publisher=signalled_while_publishing(runtime), install_signals=False),
    )

    line = runtime.sink.find("worker stopped")
    assert line["reason"] == "SIGTERM"
    assert line["signals_received"] == 1
    assert line["exit_code"] == EXIT_OK


def test_finishes_a_retrying_job_before_stopping(tmp_path: Path) -> None:
    # A signal during a failed attempt still leaves the queue consistent: the retry
    # envelope is enqueued and the original acked, so the job is neither lost nor
    # duplicated, and the loop stops without claiming the retry.
    runtime = Runtime(tmp_path)
    runtime.enqueue(make_job())

    def fails_and_signals(job: PublishJob) -> tuple[PlatformResult, ...]:
        runtime.flag.handle(signal.SIGTERM)
        return tuple(
            PlatformResult(
                platform=platform,
                status="failed",
                duration_ms=1,
                detail="simulated publish failure",
            )
            for platform in job.platforms
        )

    exit_code = run_worker(
        runtime.config(),
        runtime.deps(publisher=fails_and_signals, install_signals=False),
    )

    assert exit_code == EXIT_OK
    assert runtime.processing() == []
    assert len(runtime.queued()) == 1
    # One backoff wait, spent inside the iteration that was already in flight.
    assert runtime.sleeps == [1.0]


# --- startup and the second stop-flag seam (Requirements 3.6, 3.7) ------------


def test_installs_the_signal_handler_before_the_queue_client_exists(tmp_path: Path) -> None:
    # A worker that can consume before it can be told to stop is a worker that can
    # lose an in-flight job.
    runtime = Runtime(tmp_path)
    runtime.enqueue(make_job())

    run_worker(
        runtime.config(),
        runtime.deps(publisher=signalled_while_publishing(runtime)),
    )

    assert runtime.events[:3] == [
        f"register:{signal_name(SHUTDOWN_SIGNALS[0])}",
        f"register:{signal_name(SHUTDOWN_SIGNALS[1])}",
        "create_queue",
    ]
    assert runtime.signals.registered == list(SHUTDOWN_SIGNALS)


def test_a_signal_during_the_startup_wait_exits_cleanly(tmp_path: Path) -> None:
    # A pod deleted while its queue is still unreachable has to exit rather than
    # sit out a backoff it will never finish. Nothing was claimed, so this is a
    # clean stop, not a failure.
    runtime = Runtime(tmp_path)
    flag = runtime.flag

    class Unreachable(FakeRedis):
        def llen(self, name: str) -> int:
            flag.handle(signal.SIGTERM)
            raise ConnectionError("Connection refused")

    runtime.queue_redis = Unreachable()

    exit_code = run_worker(runtime.config(), runtime.deps(install_signals=False))

    assert exit_code == EXIT_OK
    assert "job loop started" not in runtime.sink.events()
    assert runtime.readiness.ready is False
    assert runtime.queue_redis.closed is True


def test_a_bounded_startup_wait_that_runs_out_exits_non_zero(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path)

    class Unreachable(FakeRedis):
        def llen(self, name: str) -> int:
            raise ConnectionError("Connection refused")

    runtime.queue_redis = Unreachable()

    exit_code = run_worker(
        runtime.config(),
        runtime.deps(install_signals=False, connect_max_attempts=2),
    )

    assert exit_code == EXIT_FAILURE
    assert runtime.sleeps == [0.5]
    assert runtime.readiness.ready is False
    assert runtime.queue_redis.closed is True


def test_logs_the_shutdown_budget_at_startup(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path)
    runtime.enqueue(make_job())

    run_worker(
        runtime.config(),
        runtime.deps(publisher=signalled_while_publishing(runtime), install_signals=False),
    )

    line = runtime.sink.find("worker starting")
    assert line["shutdown_budget_seconds"] == worst_case_shutdown_seconds(runtime.config())
    assert line["backend"] == "redis"
    assert line["readiness_file"] == str(runtime.readiness.path)


# --- failure paths ------------------------------------------------------------


def test_a_bad_configuration_key_exits_non_zero_naming_the_variable() -> None:
    stderr = io.StringIO()

    exit_code = main(
        RuntimeDeps(env={**BASE_ENV, "MAX_ATTEMPTS": "fifteen"}, stderr=stderr)
    )

    assert exit_code == EXIT_FAILURE
    written = stderr.getvalue()
    assert "MAX_ATTEMPTS" in written
    # One readable line, not a traceback.
    assert written.count("\n") == 1
    assert "Traceback" not in written


def test_main_runs_the_worker_when_the_configuration_is_valid(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path)
    runtime.enqueue(make_job())

    exit_code = main(
        runtime.deps(publisher=signalled_while_publishing(runtime), install_signals=False)
    )

    assert exit_code == EXIT_OK
    assert runtime.record()["status"] == "published"


def test_an_exception_from_the_loop_still_closes_connections_and_exits_non_zero(
    tmp_path: Path,
) -> None:
    # The job loop lets an infrastructure failure escape on purpose, so that the
    # message stays claimed and the reaper recovers it. What must not happen is a
    # traceback with two connections left open.
    runtime = Runtime(tmp_path)

    class Broken(RedisQueueClient):
        def receive(self, wait_seconds: float) -> ReceivedJob | None:
            raise ConnectionResetError("connection reset by peer")

    def create_queue(_config: QueueConfig) -> RedisQueueClient:
        return Broken(runtime.queue_redis)

    exit_code = run_worker(
        runtime.config(),
        runtime.deps(install_signals=False, create_queue=create_queue),
    )

    assert exit_code == EXIT_FAILURE
    assert runtime.sink.find("worker failed")["error_type"] == "ConnectionResetError"
    assert runtime.queue_redis.closed is True
    assert runtime.post_redis.closed is True
    assert runtime.readiness.ready is False


def test_a_failed_constructor_closes_what_was_already_built(tmp_path: Path) -> None:
    # Each connection is registered for closing as it is built, so the queue client
    # created a line earlier is not left open by the failure below.
    runtime = Runtime(tmp_path)

    def create_redis(_url: str) -> FakeRedis:
        raise ConnectionError("nodename nor servname provided")

    exit_code = run_worker(
        runtime.config(),
        runtime.deps(install_signals=False, create_redis=create_redis),
    )

    assert exit_code == EXIT_FAILURE
    assert runtime.queue_redis.closed is True
    assert runtime.sink.find("worker failed")["error_type"] == "ConnectionError"
    assert "queue reachable" not in runtime.sink.events()


def test_a_connection_that_refuses_to_close_does_not_change_the_exit_code(
    tmp_path: Path,
) -> None:
    runtime = Runtime(tmp_path)
    runtime.enqueue(make_job())

    class Stubborn(RedisQueueClient):
        def close(self) -> None:
            raise ConnectionError("connection already gone")

    def create_queue(_config: QueueConfig) -> RedisQueueClient:
        return Stubborn(runtime.queue_redis)

    exit_code = run_worker(
        runtime.config(),
        runtime.deps(
            publisher=signalled_while_publishing(runtime),
            install_signals=False,
            create_queue=create_queue,
        ),
    )

    assert exit_code == EXIT_OK
    assert runtime.sink.find("failed to close dependency during shutdown")["resource"] == "queue"
    # The connection after the failed one is still closed.
    assert runtime.post_redis.closed is True


# --- an injected post store skips the second connection -----------------------


def test_uses_an_injected_post_store_without_opening_a_redis_connection(
    tmp_path: Path,
) -> None:
    runtime = Runtime(tmp_path)
    runtime.enqueue(make_job())

    exit_code = run_worker(
        runtime.config(),
        runtime.deps(
            publisher=signalled_while_publishing(runtime),
            install_signals=False,
            post_store=RedisPostStore(runtime.queue_redis),
        ),
    )

    assert exit_code == EXIT_OK
    assert "create_redis" not in runtime.events
    assert runtime.queue_redis.fields(RECORD_KEY)["status"] == "published"
    assert [line["resource"] for line in runtime.sink.all("dependency closed")] == ["queue"]
