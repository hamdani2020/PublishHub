"""
Simulated publishing (Requirement 3.1, and the scope boundary that makes it
honest).

Publishing to real social platforms is out of scope for PublishHub — no
third-party API is integrated. What the worker does instead is spend
`SIMULATE_LATENCY_MS` per platform and fail with probability
`SIMULATE_FAILURE_RATE`. Those two knobs are the whole point: without them the
retry path (task 4.3), the dead-letter path, the KEDA scaling demo, and the
canary analysis have nothing to react to.

The publisher is a callable, `(PublishJob) -> tuple[PlatformResult, ...]`, so the
job loop does not know that publishing is simulated. When this project grows a
real integration, it replaces this callable and the loop is untouched.

Every source of nondeterminism — the clock, the sleep, the random draw — arrives
through `SimulatorDeps`, so a test asserts on latency and on failure without
sleeping and without a flaky coin flip.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ..config import SimulationConfig
from ..posts import PlatformResult
from ..queue import Platform, PublishJob


class Publisher(Protocol):
    """
    What the job loop needs from a publisher: results for the job's platforms, in
    the job's order, one per platform.
    """

    def __call__(self, job: PublishJob) -> tuple[PlatformResult, ...]: ...


@dataclass(frozen=True, slots=True)
class SimulatorDeps:
    """Construction seams. Defaults are the real clock, sleep, and RNG."""

    #: Blocks for the given number of seconds. The worker is single-job-at-a-time
    #: and synchronous, so a plain sleep is the accurate simulation of work.
    sleep: Callable[[float], None] = time.sleep
    #: Draw in `[0.0, 1.0)`, compared against the configured failure rate.
    random: Callable[[], float] = random.random
    #: Monotonic seconds, for measuring duration. Monotonic rather than wall clock
    #: so an NTP step cannot produce a negative duration.
    monotonic: Callable[[], float] = time.monotonic


class SimulatedPublisher:
    """
    Publishes to each platform in turn, spending the configured latency and
    failing at the configured rate.

    Platforms are processed sequentially and independently: one platform's
    simulated failure does not stop the ones after it, so a job can end up
    partially published, which is a state the API's status vocabulary already
    names and the demo should be able to produce.
    """

    __slots__ = ("_deps", "_simulation")

    def __init__(self, simulation: SimulationConfig, deps: SimulatorDeps | None = None) -> None:
        self._simulation = simulation
        self._deps = SimulatorDeps() if deps is None else deps

    def __call__(self, job: PublishJob) -> tuple[PlatformResult, ...]:
        return tuple(self.publish_to(platform) for platform in job.platforms)

    def publish_to(self, platform: Platform) -> PlatformResult:
        started = self._deps.monotonic()
        self._deps.sleep(self._simulation.latency_ms / 1000)
        duration_ms = round((self._deps.monotonic() - started) * 1000)

        if self._failed():
            return PlatformResult(
                platform=platform,
                status="failed",
                duration_ms=duration_ms,
                # Names the knob, so nobody debugs a deliberate failure as a bug.
                detail=(
                    "simulated publish failure "
                    f"(SIMULATE_FAILURE_RATE={self._simulation.failure_rate})"
                ),
            )

        return PlatformResult(platform=platform, status="published", duration_ms=duration_ms)

    def _failed(self) -> bool:
        # Short-circuit at rate 0 — the default, and the only rate a normal local
        # run uses — so the common path draws no random number at all.
        if self._simulation.failure_rate <= 0:
            return False
        return self._deps.random() < self._simulation.failure_rate


def total_duration_ms(results: Sequence[PlatformResult]) -> int:
    """Sum of the per-platform durations. Sequential publishing, so a sum is the total."""
    return sum(result.duration_ms for result in results)
