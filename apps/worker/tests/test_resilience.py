"""
Backoff, startup connection retry, and readiness reporting tests
(Requirements 3.3, 3.7).

The behavior that matters: a worker that starts while its queue is unreachable
keeps running, keeps retrying on a growing delay, and reports itself unready until
the queue answers — rather than dying on the first command and crash-looping.

The connection wait is exercised through the real `RedisQueueClient` against a fake
whose `LLEN` refuses, so what is tested is the client the worker actually uses. No
test opens a socket and no test sleeps: the wait is injected and recorded.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from publishhub_worker.config import WorkerConfig, load_config
from publishhub_worker.logging import LoggerDeps, WorkerLogger, create_logger
from publishhub_worker.queue import RedisQueueClient
from publishhub_worker.queue.testing import FakeRedis
from publishhub_worker.resilience import (
    DEFAULT_CONNECT_BACKOFF,
    DEFAULT_RETRY_BACKOFF,
    BackoffPolicy,
    ConnectDeps,
    ReadinessFile,
    wait_for_queue,
)


class RefusingRedis(FakeRedis):
    """
    A fake Redis that refuses the first `failures` `LLEN` calls, the way a client
    behaves while the server is not accepting connections yet.
    """

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.remaining = failures
        self.llen_calls = 0

    def llen(self, name: str) -> int:
        self.llen_calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise ConnectionError(
                "Error 111 connecting to publishhub-redis:6379. Connection refused."
            )
        return super().llen(name)


class Sink:
    """Captures what a wait logged, as parsed JSON objects."""

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


def make_config() -> WorkerConfig:
    return load_config({"DD_ENV": "development"})


def make_logger(stream: io.StringIO, name: str) -> WorkerLogger:
    return create_logger(make_config(), LoggerDeps(stream=stream, name=name))


def build(*, failures: int) -> tuple[RedisQueueClient, RefusingRedis, WorkerLogger, Sink]:
    redis = RefusingRedis(failures)
    stream = io.StringIO()
    return (
        RedisQueueClient(redis),
        redis,
        make_logger(stream, f"test-resilience-{failures}"),
        Sink(stream),
    )


# --- the delay schedule (Requirement 3.3) -------------------------------------


def test_the_delay_doubles_with_each_failed_attempt() -> None:
    policy = BackoffPolicy(base_seconds=1.0, multiplier=2.0, max_seconds=100.0)

    assert policy.schedule(5) == (1.0, 2.0, 4.0, 8.0, 16.0)


def test_the_delay_stops_growing_at_the_ceiling() -> None:
    # The ceiling is what bounds the worst case at MAX_ATTEMPTS=10 to minutes
    # rather than hours.
    policy = BackoffPolicy(base_seconds=1.0, multiplier=2.0, max_seconds=5.0)

    assert policy.schedule(6) == (1.0, 2.0, 4.0, 5.0, 5.0, 5.0)


def test_the_retry_default_matches_the_documented_three_attempt_schedule() -> None:
    assert DEFAULT_RETRY_BACKOFF.schedule(2) == (1.0, 2.0)


def test_the_startup_default_retries_quickly_before_backing_off() -> None:
    assert DEFAULT_CONNECT_BACKOFF.schedule(3) == (0.5, 1.0, 2.0)


@pytest.mark.parametrize(
    ("field", "value"),
    [("base_seconds", -1.0), ("multiplier", 0.5), ("max_seconds", 0.1)],
)
def test_rejects_a_policy_that_would_shrink_or_invert_the_delay(field: str, value: float) -> None:
    with pytest.raises(ValueError, match=field):
        BackoffPolicy(**{field: value})


def test_rejects_an_attempt_number_below_one() -> None:
    # `attempt` is one-based, matching the message envelope's field.
    with pytest.raises(ValueError, match="attempt"):
        DEFAULT_RETRY_BACKOFF.delay_for(0)


# --- waiting for the queue at startup (Requirement 3.7) -----------------------


def test_returns_immediately_when_the_queue_answers_on_the_first_probe() -> None:
    queue, redis, logger, sink = build(failures=0)
    sleeps: list[float] = []

    result = wait_for_queue(queue=queue, logger=logger, deps=ConnectDeps(sleep=sleeps.append))

    assert result.connected is True
    assert result.attempts == 1
    assert result.depth == 0
    assert sleeps == []
    assert redis.llen_calls == 1
    assert sink.find("queue reachable")["attempts"] == 1


def test_keeps_retrying_on_a_growing_delay_until_the_queue_answers() -> None:
    queue, _redis, logger, sink = build(failures=3)
    sleeps: list[float] = []

    result = wait_for_queue(queue=queue, logger=logger, deps=ConnectDeps(sleep=sleeps.append))

    assert result.connected is True
    assert result.attempts == 4
    assert sleeps == [0.5, 1.0, 2.0]
    assert result.waited_seconds == 3.5
    assert len(sink.all("queue unreachable, retrying")) == 3


def test_names_the_error_so_a_wrong_url_is_distinguishable_from_a_slow_start() -> None:
    queue, _redis, logger, sink = build(failures=1)

    wait_for_queue(queue=queue, logger=logger, deps=ConnectDeps(sleep=lambda _s: None))

    line = sink.find("queue unreachable, retrying")
    assert line["level"] == "warn"
    assert line["attempt"] == 1
    assert line["retry_in_seconds"] == 0.5
    assert line["error_type"] == "ConnectionError"
    assert "Connection refused" in line["error_message"]


def test_probes_with_the_same_depth_the_autoscaler_reads() -> None:
    queue, redis, logger, _sink = build(failures=0)
    redis.lists["publishhub:jobs"] = ["a", "b"]

    result = wait_for_queue(queue=queue, logger=logger, deps=ConnectDeps(sleep=lambda _s: None))

    assert result.depth == 2


def test_gives_up_only_when_a_caller_asks_for_a_bounded_wait() -> None:
    queue, _redis, logger, sink = build(failures=10)
    sleeps: list[float] = []

    result = wait_for_queue(
        queue=queue,
        logger=logger,
        deps=ConnectDeps(sleep=sleeps.append, max_attempts=3),
    )

    assert result.connected is False
    assert result.attempts == 3
    assert isinstance(result.error, ConnectionError)
    # No wait after the attempt that gave up.
    assert sleeps == [0.5, 1.0]
    assert sink.find("queue unreachable, giving up")["attempts"] == 3


def test_stops_waiting_when_the_stop_condition_fires() -> None:
    # The seam graceful shutdown (task 4.4) plugs a SIGTERM flag into, so a pod
    # deleted while waiting on a dependency exits instead of sitting out the
    # backoff.
    queue, redis, logger, sink = build(failures=10)

    result = wait_for_queue(
        queue=queue,
        logger=logger,
        deps=ConnectDeps(sleep=lambda _s: None, should_continue=lambda: False),
    )

    assert result.connected is False
    assert result.attempts == 0
    assert redis.llen_calls == 0
    assert "queue connection abandoned before the queue became reachable" in sink.events()


# --- readiness reporting (Requirement 3.7) ------------------------------------


def test_marks_ready_only_once_the_queue_has_answered(tmp_path: Path) -> None:
    queue, redis, logger, _sink = build(failures=2)
    readiness = ReadinessFile(tmp_path / "worker.ready")
    observed: list[bool] = []
    original_llen = redis.llen

    def watched_llen(name: str) -> int:
        # Sampled from inside the probe, so the unready window is observed rather
        # than inferred from the end state.
        observed.append(readiness.ready)
        return original_llen(name)

    redis.llen = watched_llen  # type: ignore[method-assign]

    result = wait_for_queue(
        queue=queue,
        logger=logger,
        readiness=readiness,
        deps=ConnectDeps(sleep=lambda _s: None),
    )

    assert result.connected is True
    assert observed == [False, False, False]
    assert readiness.ready is True


def test_stays_unready_while_the_queue_is_unreachable(tmp_path: Path) -> None:
    queue, _redis, logger, _sink = build(failures=5)
    readiness = ReadinessFile(tmp_path / "worker.ready")

    result = wait_for_queue(
        queue=queue,
        logger=logger,
        readiness=readiness,
        deps=ConnectDeps(sleep=lambda _s: None, max_attempts=2),
    )

    assert result.connected is False
    assert readiness.ready is False


def test_clears_a_marker_left_behind_by_a_previous_process(tmp_path: Path) -> None:
    # A restarted container can inherit the file from an emptyDir that outlived it,
    # which would report a worker as ready before it has reached anything.
    marker = tmp_path / "worker.ready"
    marker.write_text("stale\n", encoding="utf-8")
    queue, _redis, logger, _sink = build(failures=5)

    wait_for_queue(
        queue=queue,
        logger=logger,
        readiness=ReadinessFile(marker),
        deps=ConnectDeps(sleep=lambda _s: None, max_attempts=1),
    )

    assert marker.exists() is False


def test_marking_is_idempotent_in_both_directions(tmp_path: Path) -> None:
    readiness = ReadinessFile(tmp_path / "nested" / "worker.ready")

    readiness.mark_unready()  # nothing to remove
    assert readiness.ready is False

    readiness.mark_ready()
    readiness.mark_ready()
    assert readiness.ready is True
    assert readiness.path.read_text(encoding="utf-8").strip() != ""

    readiness.mark_unready()
    readiness.mark_unready()
    assert readiness.ready is False
