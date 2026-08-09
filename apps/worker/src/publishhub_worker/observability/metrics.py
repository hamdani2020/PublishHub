"""
Custom metrics for the worker (Requirements 14.4, 14.6).

The worker emits the four metrics from the design that describe work only it can
see: how many job attempts it processed, how many failed, how long each publish
took, and how deep the queue is.

| Metric                       | Type      | Tags                      |
|------------------------------|-----------|---------------------------|
| `publishhub.jobs.processed`  | counter   | `platform`, `status`, `env` |
| `publishhub.jobs.failed`     | counter   | `platform`, `status`, `env` |
| `publishhub.jobs.duration`   | histogram | `platform`, `status`, `env` |
| `publishhub.queue.depth`     | gauge     | `backend`, `env`          |

`publishhub.posts.submitted` is deliberately absent: it describes an API request,
and the API already emits it from the process that answered that request
(`apps/api/src/observability/metrics.ts`).

## Counting per platform, not per job

`publishhub.jobs.processed` increments once per platform in the job, the same way
the API counts `posts.submitted` once per requested platform. A job that targets
Twitter and LinkedIn is two publishes, and a failure rate that cannot say *which
platform* is failing is not worth alerting on. Because `failed` is counted with
the same granularity and the same tag values, `failed / processed` is still a
valid ratio — which is what the design's "worker failure rate above 5%" monitor
computes.

`status` on all three job metrics is the *disposition of the attempt*, not the
per-platform result: `published`, `partially_published`, `failed`, `retrying`, or
`invalid` for a payload that never parsed. A retried attempt therefore shows up as
processed *and* failed with `status:retrying`, which is exactly the distinction
between "failing and being retried" and "failed for good" that an operator needs.

## Why this file speaks DogStatsD directly

`dd-trace-py` has no public client for custom metrics — the API service gets one
for free from `dd-trace`'s `tracer.dogstatsd`, but its Python counterpart is
internal. The alternative is a second pinned Datadog package whose only job is to
render `name:value|type|#tags` and write it to a UDP socket, which is
`DogStatsdSink`: about thirty lines, fully unit-tested, and no image
dependency to scan and patch. Same reasoning the worker's `requirements.txt`
records for declining `tenacity`.

With `OBSERVABILITY_ENABLED=false` the sink is `INERT_SINK` and no socket is
ever created (Requirement 14.6). `WorkerMetrics.enabled` exposes which of the
two is in place, because the caller needs to know: sampling queue depth costs a
Redis `LLEN` or an SQS `GetQueueAttributes` call, and a disabled worker must not
pay for a number nobody will read.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable, Mapping
from typing import Literal, Protocol

from ..queue import Env, QueueBackend

#: Datadog metric names, exactly as the design document spells them.
JOBS_PROCESSED = "publishhub.jobs.processed"
JOBS_FAILED = "publishhub.jobs.failed"
JOBS_DURATION = "publishhub.jobs.duration"
QUEUE_DEPTH = "publishhub.queue.depth"

#: Everything this service emits. Useful in a test that asserts nothing else is.
WORKER_METRICS: tuple[str, ...] = (JOBS_PROCESSED, JOBS_FAILED, JOBS_DURATION, QUEUE_DEPTH)

#: What became of one delivery attempt. The first three are terminal post statuses
#: and match `PostStatus`; `retrying` means the attempt failed but the job is going
#: back on the queue; `invalid` means the payload never parsed, so there was no job.
JobDisposition = Literal["published", "partially_published", "failed", "retrying", "invalid"]

#: `platform` tag for an attempt with no readable platform list. A series that
#: sometimes carries the tag and sometimes does not is awkward to aggregate, so an
#: unparseable payload reports `none` rather than nothing — same choice the API
#: makes for a rejected submission.
NO_PLATFORM = "none"

#: DogStatsD metric-type suffixes.
COUNTER = "c"
GAUGE = "g"
HISTOGRAM = "h"

#: Where the Datadog Agent listens for DogStatsD by default. Overridden by
#: `DD_AGENT_HOST` and `DD_DOGSTATSD_PORT`, the variables the Agent's own Helm
#: chart sets on application pods.
DEFAULT_DOGSTATSD_HOST = "127.0.0.1"
DEFAULT_DOGSTATSD_PORT = 8125

#: Characters that would end a tag, a value, or the datagram itself. Tag values
#: come partly from configuration (`DD_ENV`), so they are sanitized rather than
#: trusted: a stray comma in an environment name would silently split one tag into
#: two, and a newline would forge a second metric.
_TAG_REPLACEMENTS = str.maketrans({",": "_", "|": "_", "#": "_", "\n": "_", "\r": "_"})


class MetricsSink(Protocol):
    """
    Where a recording goes. Implemented by `DogStatsdSink` when observability
    is on and by `INERT_SINK` when it is off, so the switch is one decision at
    startup instead of a conditional at every call site.
    """

    def increment(self, name: str, value: int, tags: Mapping[str, str]) -> None: ...

    def gauge(self, name: str, value: float, tags: Mapping[str, str]) -> None: ...

    def histogram(self, name: str, value: float, tags: Mapping[str, str]) -> None: ...


class _InertSink:
    """Discards everything. Used whenever `OBSERVABILITY_ENABLED` is false."""

    __slots__ = ()

    def increment(self, name: str, value: int, tags: Mapping[str, str]) -> None:
        return None

    def gauge(self, name: str, value: float, tags: Mapping[str, str]) -> None:
        return None

    def histogram(self, name: str, value: float, tags: Mapping[str, str]) -> None:
        return None


#: The disabled path. Identity-compared by `WorkerMetrics.enabled`, so it must stay
#: a single shared instance.
INERT_SINK: MetricsSink = _InertSink()

#: Sends one datagram. Injected so the protocol is asserted without a socket.
DatagramSender = Callable[[bytes], None]


def sanitize_tag(value: str) -> str:
    """A tag key or value with the protocol's delimiters neutralized."""
    return value.translate(_TAG_REPLACEMENTS)


def render_value(value: float) -> str:
    """
    `1` rather than `1.0` for a whole number, so a counter line reads the way a
    DogStatsD line normally does.
    """
    if isinstance(value, int):
        return str(value)
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def format_datagram(
    name: str,
    value: float,
    metric_type: str,
    tags: Mapping[str, str],
) -> str:
    """
    One DogStatsD line: `publishhub.jobs.processed:1|c|#platform:twitter,env:prod`.

    Tags are omitted entirely when there are none, rather than leaving a trailing
    `|#`, which some parsers read as a single empty tag.
    """
    line = f"{name}:{render_value(value)}|{metric_type}"
    if not tags:
        return line
    rendered = ",".join(
        f"{sanitize_tag(key)}:{sanitize_tag(str(tag))}" for key, tag in tags.items()
    )
    return f"{line}|#{rendered}"


class UdpDatagramSender:
    """
    Fire-and-forget UDP to the local Datadog Agent.

    The socket is created on first use, not at construction, so building the sink
    costs nothing until something is actually recorded. A send failure is counted
    and swallowed: an Agent that is not listening, or a socket that cannot be
    created at all, must not turn a metric into a failed job.
    """

    __slots__ = ("_address", "_create_socket", "_socket", "failed_sends")

    def __init__(
        self,
        *,
        host: str = DEFAULT_DOGSTATSD_HOST,
        port: int = DEFAULT_DOGSTATSD_PORT,
        create_socket: Callable[[], socket.socket] | None = None,
    ) -> None:
        self._address = (host, port)
        self._create_socket = (
            (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            if create_socket is None
            else create_socket
        )
        self._socket: socket.socket | None = None
        #: Datagrams that could not be sent. Surfaced for debugging, never raised.
        self.failed_sends = 0

    @property
    def address(self) -> tuple[str, int]:
        return self._address

    def __call__(self, payload: bytes) -> None:
        try:
            if self._socket is None:
                self._socket = self._create_socket()
            self._socket.sendto(payload, self._address)
        # Deliberately broad: a missing Agent, a full send buffer, and a sandbox
        # that forbids UDP all raise different types and none of them changes what
        # the worker should do next, which is nothing.
        except Exception:
            self.failed_sends += 1
            # Dropped so the next send rebuilds it; a socket that failed once is
            # not necessarily reusable.
            self._socket = None

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None


class DogStatsdSink:
    """Renders recordings as DogStatsD datagrams and hands them to a sender."""

    __slots__ = ("_send", "sent")

    def __init__(self, send: DatagramSender, *, keep_history: bool = False) -> None:
        self._send = send
        #: Lines sent, when `keep_history` is on. Off by default: a long-running
        #: worker must not accumulate every metric it ever emitted.
        self.sent: list[str] | None = [] if keep_history else None

    @property
    def sender(self) -> DatagramSender:
        """The sender this sink writes through. Exposed so a test can address it."""
        return self._send

    def increment(self, name: str, value: int, tags: Mapping[str, str]) -> None:
        self._emit(name, value, COUNTER, tags)

    def gauge(self, name: str, value: float, tags: Mapping[str, str]) -> None:
        self._emit(name, value, GAUGE, tags)

    def histogram(self, name: str, value: float, tags: Mapping[str, str]) -> None:
        self._emit(name, value, HISTOGRAM, tags)

    def _emit(
        self,
        name: str,
        value: float,
        metric_type: str,
        tags: Mapping[str, str],
    ) -> None:
        line = format_datagram(name, value, metric_type, tags)
        if self.sent is not None:
            self.sent.append(line)
        self._send(line.encode("utf-8"))


def create_dogstatsd_sink(
    env: Env,
    *,
    create_socket: Callable[[], socket.socket] | None = None,
) -> DogStatsdSink:
    """
    A sink pointed at the Agent named by `DD_AGENT_HOST` and `DD_DOGSTATSD_PORT`.

    Read straight from the environment rather than from `WorkerConfig`: these two
    variables address the metric transport, not the worker's behavior, and a
    malformed port is not a reason to refuse to publish posts — an unparseable
    value falls back to the default port instead of raising.
    """
    host = (env.get("DD_AGENT_HOST") or "").strip() or DEFAULT_DOGSTATSD_HOST
    raw_port = (env.get("DD_DOGSTATSD_PORT") or "").strip()
    port = int(raw_port) if raw_port.isdigit() else DEFAULT_DOGSTATSD_PORT
    return DogStatsdSink(UdpDatagramSender(host=host, port=port, create_socket=create_socket))


class WorkerMetrics:
    """
    The four recordings the worker makes, with the metric names and the tag sets
    owned here so no caller spells either out.
    """

    __slots__ = ("_env", "_sink")

    def __init__(self, *, env: str, sink: MetricsSink | None = None) -> None:
        self._env = env
        self._sink = INERT_SINK if sink is None else sink

    @property
    def enabled(self) -> bool:
        """
        Whether a recording goes anywhere. False means the caller should also skip
        the work of *producing* a value — see `QueueDepthSampler`.
        """
        return self._sink is not INERT_SINK

    @property
    def sink(self) -> MetricsSink:
        return self._sink

    def job_processed(self, *, platform: str, status: JobDisposition) -> None:
        """One delivery attempt, for one platform, whatever the result."""
        self._sink.increment(JOBS_PROCESSED, 1, self._job_tags(platform, status))

    def job_failed(self, *, platform: str, status: JobDisposition) -> None:
        """A platform that did not publish on this attempt."""
        self._sink.increment(JOBS_FAILED, 1, self._job_tags(platform, status))

    def job_duration(self, *, platform: str, status: JobDisposition, duration_ms: int) -> None:
        """How long one platform's publish took, milliseconds."""
        self._sink.histogram(JOBS_DURATION, duration_ms, self._job_tags(platform, status))

    def queue_depth_observed(self, *, backend: QueueBackend | str, depth: int) -> None:
        """Pending messages, sampled — the same number KEDA scales on."""
        self._sink.gauge(QUEUE_DEPTH, depth, {"backend": str(backend), "env": self._env})

    def _job_tags(self, platform: str, status: JobDisposition) -> dict[str, str]:
        return {"platform": platform, "status": status, "env": self._env}


#: How often queue depth is sampled at most. A sample is a round trip to Redis or
#: to SQS, and the number is a slow-moving gauge that KEDA reads from the broker
#: anyway, so once every fifteen seconds is plenty. It also keeps an SQS-backed
#: worker from turning an idle poll loop into a stream of billable
#: `GetQueueAttributes` calls.
DEFAULT_DEPTH_INTERVAL_SECONDS = 15.0


class QueueDepthSampler:
    """
    Rate-limited queue-depth sampling.

    Two things it refuses to do: sample when metrics are inert — the value would be
    discarded and the round trip wasted, which is what Requirement 14.6 means by
    "inert" — and sample more often than the configured interval.
    """

    __slots__ = ("_interval_seconds", "_last_sampled_at", "_monotonic", "backend", "metrics")

    def __init__(
        self,
        metrics: WorkerMetrics,
        *,
        backend: str,
        interval_seconds: float = DEFAULT_DEPTH_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.metrics = metrics
        self.backend = backend
        self._interval_seconds = interval_seconds
        self._monotonic = monotonic
        self._last_sampled_at: float | None = None

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def due(self) -> bool:
        if not self.metrics.enabled:
            return False
        if self._last_sampled_at is None:
            return True
        return (self._monotonic() - self._last_sampled_at) >= self._interval_seconds

    def sample(self, depth: Callable[[], int]) -> int | None:
        """
        Record the depth if one is due, and return what was recorded. `None` means
        nothing was sampled, either because metrics are inert or because the
        interval has not elapsed.

        `depth` may raise — a Redis that just went away, an SQS call that was
        throttled — and the exception is left to the caller, which owns the logger
        and is the only place that can say so usefully.
        """
        if not self.due():
            return None
        # Stamped before the call, so a slow or failing sample cannot be retried on
        # every iteration of the loop.
        self._last_sampled_at = self._monotonic()
        observed = depth()
        self.metrics.queue_depth_observed(backend=self.backend, depth=observed)
        return observed
