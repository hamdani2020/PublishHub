"""
In-memory stand-in for `SqsPort`, so SQS backend tests need no AWS account, no
credentials, and no network.

Queues are keyed by url and a sent message is delivered to that queue, the way a
real SQS queue behaves. That is what lets a test enqueue and then receive without
reaching for internals.

Mirrors `apps/api/src/queue/testing/fake-sqs-port.ts`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ..sqs_queue_client import SqsMessage


@dataclass(frozen=True, slots=True)
class SentMessage:
    queue_url: str
    message_body: str
    message_attributes: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class DeletedMessage:
    queue_url: str
    receipt_handle: str


@dataclass(frozen=True, slots=True)
class ReceiveCall:
    queue_url: str
    wait_time_seconds: int


@dataclass
class FakeSqsPort:
    """Implements the `SqsPort` protocol against dictionaries."""

    #: Queue used by `seed` and `messages` when a test does not name one.
    default_queue_url: str | None = None
    queues: dict[str, list[SqsMessage]] = field(default_factory=dict)
    sent: list[SentMessage] = field(default_factory=list)
    deleted: list[DeletedMessage] = field(default_factory=list)
    receive_calls: list[ReceiveCall] = field(default_factory=list)
    #: Value returned by `approximate_number_of_messages`.
    depth_value: int = 0
    closed: bool = False
    _counter: int = 0

    # --- helpers used by tests -------------------------------------------------

    def messages(self, queue_url: str | None = None) -> list[SqsMessage]:
        """Messages still waiting on a queue, oldest first."""
        return list(self._queue(self._resolve_queue_url(queue_url)))

    def seed(
        self,
        body: str,
        *,
        queue_url: str | None = None,
        message_id: str | None = None,
        receipt_handle: str | None = None,
    ) -> SqsMessage:
        """Put a message on a queue as if a producer had already sent it."""
        handles = self._next_handles()
        message = SqsMessage(
            message_id=handles[0] if message_id is None else message_id,
            receipt_handle=handles[1] if receipt_handle is None else receipt_handle,
            body=body,
        )
        self._queue(self._resolve_queue_url(queue_url)).append(message)
        return message

    # --- SqsPort ---------------------------------------------------------------

    def send_message(
        self,
        *,
        queue_url: str,
        message_body: str,
        message_attributes: Mapping[str, str] | None = None,
    ) -> None:
        self.sent.append(
            SentMessage(
                queue_url=queue_url,
                message_body=message_body,
                message_attributes=None if message_attributes is None else dict(message_attributes),
            )
        )
        message_id, receipt_handle = self._next_handles()
        self._queue(queue_url).append(
            SqsMessage(message_id=message_id, receipt_handle=receipt_handle, body=message_body)
        )

    def receive_message(self, *, queue_url: str, wait_time_seconds: int) -> list[SqsMessage]:
        self.receive_calls.append(
            ReceiveCall(queue_url=queue_url, wait_time_seconds=wait_time_seconds)
        )
        queue = self._queue(queue_url)
        if not queue:
            return []
        return [queue.pop(0)]

    def delete_message(self, *, queue_url: str, receipt_handle: str) -> None:
        self.deleted.append(
            DeletedMessage(queue_url=queue_url, receipt_handle=receipt_handle)
        )
        queue = self._queue(queue_url)
        for index, message in enumerate(queue):
            if message.receipt_handle == receipt_handle:
                del queue[index]
                break

    def approximate_number_of_messages(self, queue_url: str) -> int:
        return self.depth_value

    def close(self) -> None:
        self.closed = True

    # --- internals -------------------------------------------------------------

    def _queue(self, queue_url: str) -> list[SqsMessage]:
        return self.queues.setdefault(queue_url, [])

    def _next_handles(self) -> tuple[str, str]:
        self._counter += 1
        return f"message-{self._counter}", f"receipt-{self._counter}"

    def _resolve_queue_url(self, queue_url: str | None) -> str:
        resolved = queue_url if queue_url is not None else self.default_queue_url
        if resolved is None:
            raise ValueError(
                "FakeSqsPort needs a queue url: set default_queue_url or name one per call"
            )
        return resolved
