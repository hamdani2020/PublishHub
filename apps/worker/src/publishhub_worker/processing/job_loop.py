"""
The job processing loop (Requirements 3.1, 3.2, 3.3, 3.4).

Step for step, this is section 4 of the design document:

1. Reap stale `processing` entries — Redis backend only.
2. `receive(POLL_WAIT_SECONDS)`. `None` means the window elapsed with nothing on
   the queue; loop again.
3. Simulate a publish for each of the job's platforms.
4. On success, write the terminal post status to Redis and ack the message.
5. On failure, back off, re-enqueue with `attempt + 1`, and ack the original —
   until `MAX_ATTEMPTS`, at which point the post gets its terminal status and the
   message goes to the dead-letter destination.
6. On a payload that cannot be read at all, dead-letter it immediately.

Observability rides along the same path rather than threading through it. One
`with` in `run_once` wraps the whole attempt in a span continued from the
envelope's `trace_context` (Requirement 14.2), and the `JobOutcome` that comes back
is what the metrics are derived from (Requirement 14.4) — so the outcome logic
below is unchanged, and reading it does not require reading the instrumentation.
With `OBSERVABILITY_ENABLED` off both are inert: no span, no datagram, and no extra
call to the broker (Requirement 14.6).

## Why the loop does not sleep when it is idle

Requirement 3.2 is about CPU, not about correctness: a worker that polls in a
tight loop burns a core doing nothing, and with KEDA scale-to-zero that cost is
multiplied by every replica that has not yet been scaled away. So the wait happens
*inside* `receive` — `BRPOPLPUSH` with a timeout on Redis, long-polling
`ReceiveMessage` on SQS — and the idle path contains no sleep at all. An idle
worker makes one blocking call per `POLL_WAIT_SECONDS` and is otherwise parked in
the kernel. `tests/test_job_loop.py` asserts exactly that, by giving the loop a
sleep function that fails the test if the idle path calls it.

The retry path is the one place that does sleep, and it sleeps through
`JobLoopDeps.sleep` so that tests assert the schedule instead of waiting it out.

## Where retry state lives

In the message, not in the worker. A retry is a *new envelope* carrying
`attempt + 1`, a refreshed `enqueued_at`, and the same `job_id`
(`docs/message-schema.md`), enqueued after the backoff wait and followed by an ack
of the original. Two consequences that are the reason for doing it this way:

- A worker killed during a backoff wait loses nothing. The original message is
  still in the processing list, and the reaper returns it after the visibility
  window. Retry counting in worker memory would have been reset by that restart.
- Any replica can pick the retry up, so a job is not pinned to the worker that
  first failed it.

The ordering is enqueue-then-ack, for the same reason `dead_letter` pushes before
removing: a crash between the two leaves a duplicate, which at-least-once delivery
already tolerates, where the opposite order would drop the job.

## What still propagates

An unexpected exception from Redis or SQS itself — a refused post-record write, a
failed ack — is deliberately allowed to escape `run`. It means the infrastructure
the retry path depends on is the thing that is broken, so re-enqueueing through it
would fail too. The message stays in `processing`, the container restarts, and the
reaper recovers it. A failure from the *publisher*, by contrast, is exactly what
the retry path is for, so that one is caught (Requirement 3.3).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from ..config import WorkerConfig
from ..logging import WorkerLogger
from ..observability import (
    DEFAULT_DEPTH_INTERVAL_SECONDS,
    INERT_OBSERVABILITY,
    NO_PLATFORM,
    JobDisposition,
    QueueDepthSampler,
    WorkerObservability,
)
from ..posts import PlatformResult, PostStatus, PostStore
from ..queue import (
    DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    DeadLetterReason,
    PublishJob,
    QueueClient,
    ReapResult,
    ReceivedJob,
    RedisQueueClient,
    format_enqueued_at,
)
from ..resilience import DEFAULT_RETRY_BACKOFF, BackoffPolicy, always_continue
from .simulator import Publisher, SimulatedPublisher, total_duration_ms

#: How much of an unreadable payload goes into the log line. Enough to recognize
#: the message, short enough that a megabyte of garbage cannot flood the log
#: pipeline — the design's "log the raw payload truncated".
RAW_PAYLOAD_LOG_LIMIT = 200

#: Reason recorded when a job runs out of delivery attempts.
MAX_ATTEMPTS_EXHAUSTED: DeadLetterReason = "max_attempts_exhausted"

#: Fallback reason for a payload `receive` rejected without naming why. The queue
#: clients always set `invalid_reason`, so this is belt-and-braces: dead-lettering
#: with a slightly wrong reason beats crashing on `None`.
UNPARSEABLE_PAYLOAD: DeadLetterReason = "unparseable_payload"


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, kw_only=True, slots=True)
class JobOutcome:
    """
    What one pass over one message did. Returned rather than only logged so the
    loop is testable a single iteration at a time.
    """

    job: PublishJob | None
    results: tuple[PlatformResult, ...] = ()
    #: The status written to the post record, or `None` when nothing was written.
    #: A retry writes nothing: the post is still queued, which is the truth.
    post_status: PostStatus | None = None
    #: Whether the claimed message was acked, i.e. removed from the queue without
    #: being dead-lettered. True on success and on a re-enqueued retry.
    acked: bool = False
    #: Total simulated publish time across the job's platforms.
    duration_ms: int = 0
    #: Set when the job was re-enqueued for another attempt.
    retried: bool = False
    #: Backoff actually waited before re-enqueueing, `None` when there was no retry.
    retry_delay_seconds: float | None = None
    #: `attempt` on the re-enqueued envelope.
    next_attempt: int | None = None
    #: Whether the message was handed to the dead-letter destination. On SQS
    #: without an explicit `SQS_DLQ_URL` that means the redrive policy will move
    #: it, so the message is not deleted here.
    dead_lettered: bool = False
    dead_letter_reason: DeadLetterReason | None = None

    @property
    def published(self) -> bool:
        return self.post_status == "published"


@dataclass(frozen=True, kw_only=True, slots=True)
class JobLoopDeps:
    """Construction seams, so the loop is exercised without Redis, AWS, or a clock."""

    #: Defaults to a `SimulatedPublisher` built from `config.simulation`.
    publisher: Publisher | None = None
    #: Wall-clock now, for the post record's `updated_at` and the retry envelope's
    #: `enqueued_at`.
    now: Callable[[], datetime] = utc_now
    #: Stop condition, checked before each receive. The seam graceful shutdown
    #: (task 4.4) plugs a `SIGTERM` flag into.
    should_continue: Callable[[], bool] = always_continue
    #: Staleness window for the startup reaper.
    visibility_timeout_seconds: float = DEFAULT_VISIBILITY_TIMEOUT_SECONDS
    #: Delay schedule between delivery attempts (Requirement 3.3).
    retry_backoff: BackoffPolicy = DEFAULT_RETRY_BACKOFF
    #: How the retry path waits. Injected so no test spends the backoff.
    sleep: Callable[[float], None] = time.sleep
    #: Metrics and tracing. Inert by default, which is what makes the switch a
    #: startup decision rather than a branch in this file (Requirement 14.6).
    observability: WorkerObservability = INERT_OBSERVABILITY
    #: Ceiling on how often queue depth is sampled. Only consulted when metrics are
    #: live, because sampling costs a round trip to the broker.
    depth_interval_seconds: float = DEFAULT_DEPTH_INTERVAL_SECONDS
    #: Monotonic clock behind the depth sampler's rate limit.
    monotonic: Callable[[], float] = time.monotonic


class JobLoop:
    """Claim, publish, record, ack — or retry, or dead-letter — one job at a time."""

    __slots__ = (
        "_config",
        "_deps",
        "_depth",
        "_logger",
        "_observability",
        "_post_store",
        "_publisher",
        "_queue",
    )

    def __init__(
        self,
        *,
        config: WorkerConfig,
        queue: QueueClient,
        post_store: PostStore,
        logger: WorkerLogger,
        deps: JobLoopDeps | None = None,
    ) -> None:
        self._config = config
        self._queue = queue
        self._post_store = post_store
        self._logger = logger
        self._deps = JobLoopDeps() if deps is None else deps
        self._publisher: Publisher = (
            SimulatedPublisher(config.simulation)
            if self._deps.publisher is None
            else self._deps.publisher
        )
        self._observability = self._deps.observability
        self._depth = QueueDepthSampler(
            self._observability.metrics,
            backend=config.queue.backend,
            interval_seconds=self._deps.depth_interval_seconds,
            monotonic=self._deps.monotonic,
        )

    def run(self, *, max_iterations: int | None = None) -> None:
        """
        Reap, then loop until the stop condition says otherwise.

        `max_iterations` bounds the number of receives, which is what makes the
        loop testable: production passes `None` and the worker runs until
        `SIGTERM`.

        Reaching the queue in the first place is not this method's job. The
        startup wait lives in `publishhub_worker.resilience.wait_for_queue`, which
        the process entrypoint calls before constructing the loop (Requirement
        3.7), so a loop that is running has already had one successful round trip
        to the queue.
        """
        self.reap_abandoned_jobs()

        self._logger.info(
            "job loop started",
            backend=self._config.queue.backend,
            poll_wait_seconds=self._config.poll_wait_seconds,
            max_attempts=self._config.max_attempts,
            retry_backoff_seconds=list(
                self._deps.retry_backoff.schedule(max(0, self._config.max_attempts - 1))
            ),
            simulate_latency_ms=self._config.simulation.latency_ms,
            simulate_failure_rate=self._config.simulation.failure_rate,
            # Logged because "why are there no metrics" is a question with two
            # answers, and this line rules one of them out.
            observability_enabled=self._observability.enabled,
            tracing_enabled=self._observability.tracing.enabled,
        )

        iterations = 0
        while self._deps.should_continue():
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1
            self.run_once()

        self._logger.info("job loop stopped", iterations=iterations)

    def run_once(self) -> JobOutcome | None:
        """
        One blocking receive and, if a message arrived, one job processed.
        `None` means the poll window elapsed with an empty queue.
        """
        received = self._queue.receive(self._config.poll_wait_seconds)
        self.sample_queue_depth()

        if received is None:
            # Debug, not info: at `POLL_WAIT_SECONDS=20` an idle worker would
            # otherwise write three lines a minute forever, and the log volume of
            # a scaled-to-zero-capable service should be near zero too.
            self._logger.debug("queue idle", wait_seconds=self._config.poll_wait_seconds)
            return None

        job = received.job
        # Requirement 14.2: the envelope's `trace_context` carries the API request's
        # propagation headers, so this span continues that trace instead of starting
        # a new one. `{}` — what the API sends with tracing off — yields a root span.
        with self._observability.tracing.job_span(
            trace_context={} if job is None else job.trace_context,
            resource=None if job is None else ",".join(job.platforms),
            tags=_span_tags(job, self._config.queue.backend),
        ) as span:
            outcome = self._process(received)
            span.set_tags(**_outcome_span_tags(outcome))

        self._record_outcome(outcome)
        return outcome

    def sample_queue_depth(self) -> int | None:
        """
        Record `publishhub.queue.depth` if a sample is due (Requirement 14.4).

        Returns the depth that was recorded, or `None` when nothing was sampled —
        which is every call when metrics are inert, so a worker with observability
        off issues no extra broker calls at all (Requirement 14.6).

        A failed sample is logged and swallowed. The depth is a gauge nobody's
        correctness depends on, and the receive that just succeeded is better
        evidence about the broker than this call would be.

        Warning rather than debug, and deliberately so: the interesting failure here
        is not an outage — an outage announces itself through the receive — but a
        missing `sqs:GetQueueAttributes` permission, which breaks the design's
        queue-depth monitor while everything else looks healthy. The sampler's
        interval is what keeps that from becoming a flood.
        """
        try:
            return self._depth.sample(self._queue.depth)
        # Deliberately broad: redis-py and botocore raise unrelated types, and
        # neither should end a worker that is otherwise processing jobs fine.
        except Exception as error:
            self._logger.warning(
                "queue depth sample failed",
                error_type=type(error).__name__,
                backend=self._config.queue.backend,
            )
            return None

    def reap_abandoned_jobs(self) -> ReapResult | None:
        """
        Return `processing` entries left behind by a killed worker to the jobs
        list. Redis only: SQS does this itself, by making an unacked message
        visible again when its visibility timeout expires.

        Runs at startup, which is the moment that matters — a worker starting is
        usually a worker that just replaced one that died.
        """
        if not isinstance(self._queue, RedisQueueClient):
            return None

        result = self._queue.reap_stale_processing(self._deps.visibility_timeout_seconds)

        if result.reclaimed or result.adopted or result.pruned:
            self._logger.info(
                "reaped stale processing entries",
                reclaimed=len(result.reclaimed),
                adopted=len(result.adopted),
                pruned=len(result.pruned),
                visibility_timeout_seconds=self._deps.visibility_timeout_seconds,
            )
        return result

    def _process(self, received: ReceivedJob) -> JobOutcome:
        job = received.job
        if job is None:
            return self._dead_letter_payload(received)

        logger = self._logger.bind(job_id=job.job_id, post_id=job.post_id, attempt=job.attempt)
        results, duration_ms = self._publish(job, logger)

        if not all(result.ok for result in results):
            return self._failed(received, job, results, duration_ms, logger)

        # Status first, then ack. A crash between the two leaves a published post
        # whose message gets redelivered, and a redelivery rewrites the same
        # terminal status — harmless. The opposite order would ack a message whose
        # result was never recorded, leaving a post stuck at `queued` forever.
        self._post_store.record_status(
            job.post_id,
            status="published",
            results=results,
            moment=self._deps.now(),
        )
        self._queue.ack(received)

        logger.info(
            "job published",
            status="published",
            platforms=_platform_fields(results),
            duration_ms=duration_ms,
        )

        return JobOutcome(
            job=job,
            results=results,
            post_status="published",
            acked=True,
            duration_ms=duration_ms,
        )

    def _publish(
        self,
        job: PublishJob,
        logger: WorkerLogger,
    ) -> tuple[tuple[PlatformResult, ...], int]:
        """
        Run the publisher, turning a raised exception into failed results.

        Requirement 3.3 is written in terms of processing *raising*, and a real
        publishing integration would raise rather than return a status, so a
        thrown error has to enter the same retry path as a reported failure
        instead of taking the process down.
        """
        try:
            results = self._publisher(job)
        # Deliberately broad: whatever a publisher raises is a job failure, and
        # the retry path is where a job failure belongs.
        except Exception as error:
            logger.error("job publish raised", exc=error, error_type=type(error).__name__)
            # Type name only, never the message: `detail` is persisted on the post
            # record and served to clients by the API, so an exception's text —
            # which can carry a URL, a host name, or a credential — stays in the
            # log line above and out of the record.
            results = tuple(
                PlatformResult(
                    platform=platform,
                    status="failed",
                    duration_ms=0,
                    detail=f"publish raised {type(error).__name__}",
                )
                for platform in job.platforms
            )
            return results, 0

        return results, total_duration_ms(results)

    def _failed(
        self,
        received: ReceivedJob,
        job: PublishJob,
        results: tuple[PlatformResult, ...],
        duration_ms: int,
        logger: WorkerLogger,
    ) -> JobOutcome:
        """At least one platform did not publish: retry, or give up (Requirement 3.3)."""
        if job.attempt >= self._config.max_attempts:
            return self._exhausted(received, job, results, duration_ms, logger)
        return self._retry(received, job, results, duration_ms, logger)

    def _retry(
        self,
        received: ReceivedJob,
        job: PublishJob,
        results: tuple[PlatformResult, ...],
        duration_ms: int,
        logger: WorkerLogger,
    ) -> JobOutcome:
        """
        Wait out the backoff, put a fresh envelope on the queue with the next
        attempt number, then ack the original.

        The whole job is retried, including platforms that succeeded, so a
        partially failed job can publish twice to the platforms that worked. That
        is the honest consequence of at-least-once delivery with per-job
        granularity, and it is recorded here rather than hidden: per-platform
        retry would need per-platform state in the envelope, which the message
        schema does not have and which a simulated publisher does not justify.
        """
        delay = self._deps.retry_backoff.delay_for(job.attempt)
        next_attempt = job.attempt + 1

        logger.warning(
            "job publish failed, retrying",
            platforms=_platform_fields(results),
            duration_ms=duration_ms,
            failed=_failed_platforms(results),
            next_attempt=next_attempt,
            max_attempts=self._config.max_attempts,
            retry_in_seconds=delay,
        )

        # Held in the processing list for the whole wait, so a worker killed here
        # loses nothing: the reaper returns the original message.
        self._deps.sleep(delay)

        # Same `job_id` so every attempt of one job correlates in logs; refreshed
        # `enqueued_at` so the field keeps meaning "queued since" for this attempt.
        self._queue.enqueue(
            replace(
                job,
                attempt=next_attempt,
                enqueued_at=format_enqueued_at(self._deps.now()),
            )
        )
        self._queue.ack(received)

        return JobOutcome(
            job=job,
            results=results,
            duration_ms=duration_ms,
            acked=True,
            retried=True,
            retry_delay_seconds=delay,
            next_attempt=next_attempt,
        )

    def _exhausted(
        self,
        received: ReceivedJob,
        job: PublishJob,
        results: tuple[PlatformResult, ...],
        duration_ms: int,
        logger: WorkerLogger,
    ) -> JobOutcome:
        """
        The last attempt failed. Record the terminal status and dead-letter the
        message (Requirements 3.3, 3.4).

        `partially_published` when something got out, `failed` when nothing did:
        both are terminal, and the difference is what a client needs to know.
        Recording it here is what keeps Requirement 3.1 true for a job that never
        succeeded — a dead letter with no status change would leave the post at
        `queued` forever.
        """
        status: PostStatus = (
            "partially_published" if any(result.ok for result in results) else "failed"
        )

        # Status before dead-letter, the same ordering as the success path: a crash
        # in between leaves the message claimed, the reaper returns it, and the
        # next attempt rewrites the same terminal status.
        self._post_store.record_status(
            job.post_id,
            status=status,
            results=results,
            moment=self._deps.now(),
        )
        self._queue.dead_letter(received, MAX_ATTEMPTS_EXHAUSTED)

        logger.warning(
            "job dead-lettered",
            reason=MAX_ATTEMPTS_EXHAUSTED,
            status=status,
            platforms=_platform_fields(results),
            duration_ms=duration_ms,
            failed=_failed_platforms(results),
            max_attempts=self._config.max_attempts,
        )

        return JobOutcome(
            job=job,
            results=results,
            post_status=status,
            duration_ms=duration_ms,
            dead_lettered=True,
            dead_letter_reason=MAX_ATTEMPTS_EXHAUSTED,
        )

    def _dead_letter_payload(self, received: ReceivedJob) -> JobOutcome:
        """
        A payload `receive` could not turn into a job: not JSON, an unknown
        `schema_version`, or a field that failed validation. Dead-lettered
        immediately and never retried — a message nobody can parse will not parse
        on the fourth attempt either, and leaving it claimed would block the queue
        that Requirement 3.4 says must keep moving.

        No post status is written: without a parsed envelope there is no
        `post_id` to write it against.
        """
        reason = received.invalid_reason or UNPARSEABLE_PAYLOAD
        self._queue.dead_letter(received, reason)

        self._logger.warning(
            "job payload dead-lettered",
            reason=reason,
            detail=received.invalid_detail,
            raw=_truncate(received.raw),
            raw_length=len(received.raw),
        )

        return JobOutcome(job=None, dead_lettered=True, dead_letter_reason=reason)

    def _record_outcome(self, outcome: JobOutcome) -> None:
        """
        Emit `jobs.processed`, `jobs.failed`, and `jobs.duration` for one attempt
        (Requirement 14.4).

        Counted once per platform, because a failure rate that cannot say which
        platform is failing is not worth alerting on, and because `failed` and
        `processed` sharing a granularity is what makes their ratio meaningful. The
        `status` tag is the disposition of the *attempt*, so a retry is visible as
        `status:retrying` rather than hiding among terminal failures.
        """
        metrics = self._observability.metrics
        if not metrics.enabled:
            return

        status = job_disposition(outcome)

        if outcome.job is None:
            # No envelope, so no platform list. One recording, tagged `none`, so the
            # series still counts an attempt that consumed a message.
            metrics.job_processed(platform=NO_PLATFORM, status=status)
            metrics.job_failed(platform=NO_PLATFORM, status=status)
            return

        for result in outcome.results:
            metrics.job_processed(platform=result.platform, status=status)
            metrics.job_duration(
                platform=result.platform,
                status=status,
                duration_ms=result.duration_ms,
            )
            if not result.ok:
                metrics.job_failed(platform=result.platform, status=status)


def job_disposition(outcome: JobOutcome) -> JobDisposition:
    """
    The `status` tag for one attempt, and the vocabulary the design's worker-failure
    monitor groups by.

    `retrying` is checked before the post status on purpose: a retried attempt
    writes no terminal status, and calling it `failed` would make a transient
    failure indistinguishable from a job that gave up.
    """
    if outcome.job is None:
        return "invalid"
    if outcome.retried:
        return "retrying"
    if outcome.post_status in ("published", "partially_published", "failed"):
        return outcome.post_status
    # No terminal status and no retry: nothing this loop can currently produce, and
    # `failed` is the honest reading of an attempt that did not publish.
    return "failed"


def _span_tags(job: PublishJob | None, backend: str) -> dict[str, object]:
    """Span tags known before the job runs."""
    tags: dict[str, object] = {"queue.backend": backend}
    if job is None:
        return tags
    tags.update(
        {
            "job.id": job.job_id,
            "post.id": job.post_id,
            "job.attempt": job.attempt,
            "job.platforms": ",".join(job.platforms),
        }
    )
    return tags


def _outcome_span_tags(outcome: JobOutcome) -> dict[str, object]:
    """Span tags known only once the attempt is over."""
    tags: dict[str, object] = {
        "job.status": job_disposition(outcome),
        "job.duration_ms": outcome.duration_ms,
    }
    if outcome.post_status is not None:
        tags["post.status"] = outcome.post_status
    if outcome.dead_letter_reason is not None:
        tags["job.dead_letter_reason"] = outcome.dead_letter_reason
    if outcome.next_attempt is not None:
        tags["job.next_attempt"] = outcome.next_attempt
    return tags


def _platform_fields(results: tuple[PlatformResult, ...]) -> list[dict[str, object]]:
    """Per-platform results as log fields (Requirement 3.5)."""
    return [
        {
            "platform": result.platform,
            "status": result.status,
            "duration_ms": result.duration_ms,
            **({} if result.detail is None else {"detail": result.detail}),
        }
        for result in results
    ]


def _failed_platforms(results: tuple[PlatformResult, ...]) -> list[str]:
    return [result.platform for result in results if not result.ok]


def _truncate(raw: str, limit: int = RAW_PAYLOAD_LOG_LIMIT) -> str:
    """The payload as far as the log budget allows, with the cut made explicit."""
    if len(raw) <= limit:
        return raw
    return f"{raw[:limit]}… (truncated from {len(raw)} characters)"
