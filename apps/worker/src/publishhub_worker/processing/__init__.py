"""
Public surface of the job processing loop.

The loop and the publisher are separate on purpose: the loop owns claiming,
recording, retrying, dead-lettering, and acking, while the publisher owns "what a
publish is". Today that is a simulation; replacing it does not touch the loop.

The delay schedule the retry path waits out lives in `publishhub_worker.resilience`,
which the startup connection wait shares.
"""

from .job_loop import (
    MAX_ATTEMPTS_EXHAUSTED,
    RAW_PAYLOAD_LOG_LIMIT,
    UNPARSEABLE_PAYLOAD,
    JobLoop,
    JobLoopDeps,
    JobOutcome,
    job_disposition,
    utc_now,
)
from .simulator import (
    Publisher,
    SimulatedPublisher,
    SimulatorDeps,
    total_duration_ms,
)

__all__ = [
    "MAX_ATTEMPTS_EXHAUSTED",
    "RAW_PAYLOAD_LOG_LIMIT",
    "UNPARSEABLE_PAYLOAD",
    "JobLoop",
    "JobLoopDeps",
    "JobOutcome",
    "Publisher",
    "SimulatedPublisher",
    "SimulatorDeps",
    "job_disposition",
    "total_duration_ms",
    "utc_now",
]
