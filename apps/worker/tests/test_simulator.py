"""
Simulated publishing tests (Requirement 3.1).

Publishing is simulated — no third-party social API is integrated — so what is
worth testing is that the two knobs behave as documented: `SIMULATE_LATENCY_MS`
is spent per platform, and `SIMULATE_FAILURE_RATE` decides the outcome.

The clock, the sleep, and the random draw are all injected, so these tests neither
sleep nor flip a real coin.
"""

from __future__ import annotations

from publishhub_worker.config import load_config
from publishhub_worker.processing import SimulatedPublisher, SimulatorDeps, total_duration_ms
from publishhub_worker.queue import create_publish_job

POST_ID = "post_01HZX3QK7M9V4TDR8N2C5EAB6F"


class Recorder:
    """Records the sleeps, advances a fake monotonic clock by each one."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.now = 1_000.0
        self.draws = 0

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def draw(self, value: float = 0.0):
        def random() -> float:
            self.draws += 1
            return value

        return random


def build(
    recorder: Recorder,
    *,
    latency_ms: str = "500",
    failure_rate: str = "0",
    draw: float = 0.0,
) -> SimulatedPublisher:
    config = load_config(
        {"SIMULATE_LATENCY_MS": latency_ms, "SIMULATE_FAILURE_RATE": failure_rate}
    )
    return SimulatedPublisher(
        config.simulation,
        SimulatorDeps(
            sleep=recorder.sleep,
            random=recorder.draw(draw),
            monotonic=recorder.monotonic,
        ),
    )


def job(*platforms: str):
    return create_publish_job(post_id=POST_ID, content="hello", platforms=platforms)


def test_spends_the_configured_latency_once_per_platform() -> None:
    recorder = Recorder()

    results = build(recorder, latency_ms="500")(job("twitter", "linkedin"))

    assert recorder.sleeps == [0.5, 0.5]
    assert [result.platform for result in results] == ["twitter", "linkedin"]
    assert [result.duration_ms for result in results] == [500, 500]
    assert total_duration_ms(results) == 1000


def test_publishes_every_platform_and_draws_no_random_number_at_rate_zero() -> None:
    recorder = Recorder()

    results = build(recorder, failure_rate="0")(job("twitter", "mastodon"))

    assert all(result.ok for result in results)
    assert all(result.detail is None for result in results)
    # The default rate is 0; the common path should not even consult the RNG.
    assert recorder.draws == 0


def test_fails_and_names_the_knob_when_the_draw_falls_under_the_rate() -> None:
    recorder = Recorder()

    result = build(recorder, failure_rate="1", draw=0.99)(job("bluesky"))[0]

    assert result.ok is False
    assert result.status == "failed"
    assert "SIMULATE_FAILURE_RATE=1.0" in (result.detail or "")


def test_treats_the_rate_as_an_exclusive_upper_bound_on_the_draw() -> None:
    recorder = Recorder()

    # A draw equal to the rate must not fail: random() returns [0.0, 1.0), so
    # `draw < rate` is what makes rate 0.25 mean one in four rather than more.
    assert build(recorder, failure_rate="0.25", draw=0.25)(job("twitter"))[0].ok is True
    assert build(recorder, failure_rate="0.25", draw=0.24)(job("twitter"))[0].ok is False


def test_keeps_publishing_the_remaining_platforms_after_one_fails() -> None:
    recorder = Recorder()
    outcomes = iter([0.0, 1.0])  # first platform fails, second succeeds

    publisher = SimulatedPublisher(
        load_config({"SIMULATE_LATENCY_MS": "0", "SIMULATE_FAILURE_RATE": "0.5"}).simulation,
        SimulatorDeps(
            sleep=recorder.sleep, random=lambda: next(outcomes), monotonic=recorder.monotonic
        ),
    )

    results = publisher(job("twitter", "linkedin"))

    assert [result.status for result in results] == ["failed", "published"]


def test_a_zero_latency_configuration_does_not_sleep_at_all() -> None:
    recorder = Recorder()

    results = build(recorder, latency_ms="0")(job("twitter"))

    assert recorder.sleeps == [0.0]
    assert results[0].duration_ms == 0
