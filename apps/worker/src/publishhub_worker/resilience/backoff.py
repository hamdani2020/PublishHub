"""
Exponential backoff arithmetic (Requirements 3.3, 3.7).

One policy object, two callers: the job loop waits `delay_for(attempt)` before
re-enqueueing a failed job, and the startup connector waits the same way between
attempts to reach the queue.

## Why this is not `tenacity`

The design document names `tenacity` as the backoff library, and this module is
the deliberate departure from it. Both places PublishHub needs backoff are a
`while` loop the code already owns rather than a callable to wrap:

- The job retry is not an in-process retry at all. The delay is followed by an
  *enqueue* of a new envelope with `attempt + 1`, so the retry state lives in the
  message (`docs/message-schema.md`) and survives the worker dying mid-backoff.
  `tenacity` would only be computing the sleep interval.
- The startup connector has to keep retrying while reporting unready, and stop
  when a stop flag is set (spec task 4.4). That is a loop with two exit
  conditions, not a decorated function.

What is left is `min(base * multiplier ** (attempt - 1), max_seconds)`. A runtime
dependency shipped in the container image is a dependency to pin, scan, and patch
for as long as the project lives, and this one would buy three lines of
arithmetic. So the arithmetic is here, with the bounds validated at construction
and asserted in `tests/test_resilience.py`, and `requirements.txt` stays at the
two packages the queue clients actually need.

## Why there is no jitter

Jitter exists to decorrelate retries that a shared downstream outage lined up.
Neither delay here is correlated across replicas: a job's backoff starts when
*that* job's publish failed, and a startup delay starts when *that* pod booted.
Adding a random draw would make both harder to test for no benefit. If a real
publishing integration ever replaces the simulator, this is the place to add it.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Retry defaults for a failed job. With `MAX_ATTEMPTS=3` the two delays are 1s
#: and 2s, which is long enough to matter and short enough that a demo does not
#: look hung. The 30s ceiling bounds the worst case at the `MAX_ATTEMPTS=10`
#: configuration limit to a few minutes rather than hours.
DEFAULT_BASE_SECONDS = 1.0
DEFAULT_MULTIPLIER = 2.0
DEFAULT_MAX_SECONDS = 30.0

#: Startup defaults. The first retry is fast because the usual cause of an
#: unreachable queue is a Redis pod that is seconds from being ready; the same
#: 30s ceiling keeps a longer outage to one log line every half minute.
CONNECT_BASE_SECONDS = 0.5


@dataclass(frozen=True, kw_only=True, slots=True)
class BackoffPolicy:
    """
    An exponential delay schedule, validated at construction.

    `delay_for(1)` is the wait after the first failure, so the schedule for the
    defaults is 1s, 2s, 4s, ... capped at `max_seconds`.
    """

    base_seconds: float = DEFAULT_BASE_SECONDS
    multiplier: float = DEFAULT_MULTIPLIER
    max_seconds: float = DEFAULT_MAX_SECONDS

    def __post_init__(self) -> None:
        # These are programming errors, not configuration: none of the three
        # fields is settable from the environment, so raising is the right answer.
        if self.base_seconds < 0:
            raise ValueError(f"base_seconds must be >= 0 — received {self.base_seconds}")
        if self.multiplier < 1:
            raise ValueError(
                f"multiplier must be >= 1, or the delay shrinks — received {self.multiplier}"
            )
        if self.max_seconds < self.base_seconds:
            raise ValueError(
                f"max_seconds must be >= base_seconds — received "
                f"max_seconds={self.max_seconds}, base_seconds={self.base_seconds}"
            )

    def delay_for(self, attempt: int) -> float:
        """
        Seconds to wait after `attempt` has failed. `attempt` is one-based, the
        same numbering the message envelope's `attempt` field uses.
        """
        if attempt < 1:
            raise ValueError(f"attempt must be >= 1 — received {attempt}")
        return min(self.base_seconds * self.multiplier ** (attempt - 1), self.max_seconds)

    def schedule(self, attempts: int) -> tuple[float, ...]:
        """The first `attempts` delays. Useful in logs and in tests."""
        return tuple(self.delay_for(attempt) for attempt in range(1, max(0, attempts) + 1))


#: Backoff between delivery attempts of a job (Requirement 3.3).
DEFAULT_RETRY_BACKOFF = BackoffPolicy()

#: Backoff between attempts to reach the queue at startup (Requirement 3.7).
DEFAULT_CONNECT_BACKOFF = BackoffPolicy(base_seconds=CONNECT_BASE_SECONDS)
