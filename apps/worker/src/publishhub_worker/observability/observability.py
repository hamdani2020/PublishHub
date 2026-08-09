"""
The two observability seams, bundled (Requirement 14.6).

The job loop takes one object rather than two, because metrics and tracing are
always on or off together: they answer to the same `OBSERVABILITY_ENABLED` switch,
and a worker with traces but no metrics — or the reverse — is a configuration nobody
asked for and nothing tests.

`INERT_OBSERVABILITY` is what the loop uses by default, so every existing test and
every local run behaves exactly as it did before this module existed: no datagrams,
no spans, no extra round trips to the broker.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..config import WorkerConfig
from ..queue import Env
from .metrics import INERT_SINK, MetricsSink, WorkerMetrics, create_dogstatsd_sink
from .tracing import INERT_TRACING, Tracing


@dataclass(frozen=True, kw_only=True, slots=True)
class WorkerObservability:
    """Metrics and tracing, as the job loop receives them."""

    metrics: WorkerMetrics
    tracing: Tracing

    @property
    def enabled(self) -> bool:
        """Whether anything is being recorded at all. Logged at startup."""
        return self.metrics.enabled or self.tracing.enabled


#: The disabled path, shared. Safe to share: `WorkerMetrics` holds no mutable state,
#: and the rate limiter that does (`QueueDepthSampler`) belongs to the job loop.
INERT_OBSERVABILITY = WorkerObservability(
    metrics=WorkerMetrics(env="development", sink=INERT_SINK),
    tracing=INERT_TRACING,
)


def create_observability(
    config: WorkerConfig,
    *,
    tracing: Tracing | None = None,
    sink: MetricsSink | None = None,
    env: Env | None = None,
) -> WorkerObservability:
    """
    Build the bundle from validated configuration.

    `tracing` comes from `observability/bootstrap.py`, which had to run long before
    this call to get in front of `redis` and `botocore`; passing it in rather than
    importing it here is what keeps this module free of that side effect. `sink`
    defaults to a DogStatsD sink pointed at the local Agent, and `env` is only read
    to address it.

    With the switch off the result is `INERT_OBSERVABILITY`, whatever was passed:
    configuration is the authority on whether this worker observes itself, so an
    enabled tracer handed to a disabled worker is ignored rather than half-honored.
    """
    if not config.observability.enabled:
        return INERT_OBSERVABILITY

    resolved_sink = (
        create_dogstatsd_sink(os.environ if env is None else env) if sink is None else sink
    )
    return WorkerObservability(
        metrics=WorkerMetrics(env=config.observability.env, sink=resolved_sink),
        tracing=INERT_TRACING if tracing is None else tracing,
    )
