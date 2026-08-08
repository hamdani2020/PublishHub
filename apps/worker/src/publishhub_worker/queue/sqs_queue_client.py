"""
SQS backend — the AWS path (Requirement 5.3).

| Operation     | SQS API                                                           |
|---------------|-------------------------------------------------------------------|
| `enqueue`     | `SendMessage`                                                     |
| `receive`     | `ReceiveMessage` with long polling                                |
| `ack`         | `DeleteMessage`                                                   |
| `dead_letter` | `SendMessage` to the DLQ + `DeleteMessage`, or the redrive policy  |
| `depth`       | `GetQueueAttributes ApproximateNumberOfMessages`                  |

The message body is the same JSON text the Redis backend stores, and no contract
data is carried in message attributes, so a message captured from one backend can
be replayed into the other.

There is no reaper here: SQS has a visibility timeout of its own, so a message
claimed by a worker that dies becomes visible again without help.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .publish_job import describe_job, parse_publish_job, serialize_publish_job
from .types import (
    DeadLetterEvent,
    DeadLetterListener,
    PublishJob,
    ReceivedJob,
    SqsJobHandle,
)

#: SQS caps `WaitTimeSeconds` at 20.
SQS_MAX_WAIT_SECONDS = 20


@dataclass(frozen=True, slots=True)
class SqsMessage:
    message_id: str
    receipt_handle: str
    body: str


class SqsPort(Protocol):
    """
    The narrow slice of SQS this client uses. `AwsSqsPort` in `aws_sqs_port.py`
    implements it over `boto3`; the unit tests implement it with a fake, so no
    test needs AWS credentials.
    """

    def send_message(
        self,
        *,
        queue_url: str,
        message_body: str,
        message_attributes: Mapping[str, str] | None = None,
    ) -> None: ...

    def receive_message(self, *, queue_url: str, wait_time_seconds: int) -> list[SqsMessage]: ...

    def delete_message(self, *, queue_url: str, receipt_handle: str) -> None: ...

    def approximate_number_of_messages(self, queue_url: str) -> int: ...

    def close(self) -> None: ...


class SqsQueueClient:
    """
    SQS implementation of `QueueClient`.

    `dead_letter_queue_url` is optional. When set, `dead_letter` sends the message
    to that queue explicitly and deletes it from the main queue. When unset, the
    message is left in place for the queue's redrive policy to move after
    `maxReceiveCount` receives.
    """

    def __init__(
        self,
        sqs: SqsPort,
        *,
        queue_url: str,
        dead_letter_queue_url: str | None = None,
        on_dead_letter: DeadLetterListener | None = None,
    ) -> None:
        self._sqs = sqs
        self._queue_url = queue_url
        self._dead_letter_queue_url = dead_letter_queue_url
        self._on_dead_letter = on_dead_letter

    def enqueue(self, job: PublishJob) -> None:
        self._sqs.send_message(
            queue_url=self._queue_url,
            message_body=serialize_publish_job(job),
        )

    def receive(self, wait_seconds: float) -> ReceivedJob | None:
        wait_time_seconds = min(SQS_MAX_WAIT_SECONDS, max(0, math.trunc(wait_seconds)))

        messages = self._sqs.receive_message(
            queue_url=self._queue_url,
            wait_time_seconds=wait_time_seconds,
        )
        if not messages:
            return None

        message = messages[0]
        handle = SqsJobHandle(
            message_id=message.message_id,
            receipt_handle=message.receipt_handle,
        )
        parsed = parse_publish_job(message.body)

        if parsed.ok:
            return ReceivedJob(raw=message.body, handle=handle, job=parsed.job)
        return ReceivedJob(
            raw=message.body,
            handle=handle,
            job=None,
            invalid_reason=parsed.reason,
            invalid_detail=parsed.detail,
        )

    def ack(self, job: ReceivedJob) -> None:
        self._sqs.delete_message(
            queue_url=self._queue_url,
            receipt_handle=self._receipt_handle(job),
        )

    def dead_letter(self, job: ReceivedJob, reason: str) -> None:
        receipt_handle = self._receipt_handle(job)
        described = describe_job(job.job)

        if self._dead_letter_queue_url is not None:
            # Body unchanged so the dead letter stays replayable; the reason rides
            # along as an attribute, which no consumer reads as contract data.
            self._sqs.send_message(
                queue_url=self._dead_letter_queue_url,
                message_body=job.raw,
                message_attributes={"DeadLetterReason": reason},
            )
            self._sqs.delete_message(queue_url=self._queue_url, receipt_handle=receipt_handle)
        # Otherwise the message is deliberately left untouched: it becomes visible
        # again after the visibility timeout and the queue's redrive policy moves
        # it to the DLQ once `maxReceiveCount` is reached. Deleting it here would
        # discard it instead of dead-lettering it.

        if self._on_dead_letter is not None:
            self._on_dead_letter(
                DeadLetterEvent(
                    backend="sqs",
                    reason=reason,
                    job_id=described.job_id,
                    post_id=described.post_id,
                    attempt=described.attempt,
                    via_redrive_policy=self._dead_letter_queue_url is None,
                )
            )

    def depth(self) -> int:
        return self._sqs.approximate_number_of_messages(self._queue_url)

    def close(self) -> None:
        self._sqs.close()

    def _receipt_handle(self, job: ReceivedJob) -> str:
        if job.handle.backend != "sqs":
            raise TypeError(
                f'SqsQueueClient received a "{job.handle.backend}" handle; '
                "handles are not portable across backends"
            )
        return job.handle.receipt_handle
