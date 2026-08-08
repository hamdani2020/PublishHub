"""
The only file in the Python queue abstraction that touches boto3. Keeping the SDK
behind `SqsPort` is what lets every backend test run against a fake with no
credentials, no region, and no network.

Credentials come from the ambient AWS chain — IRSA in the cluster, the local
profile on a laptop. No access keys are read from configuration.

Mirrors `apps/api/src/queue/aws-sqs-port.ts`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .sqs_queue_client import SqsMessage


class AwsSqsPort:
    """`SqsPort` over a boto3 SQS client."""

    def __init__(self, *, region: str | None = None, client: Any | None = None) -> None:
        if client is not None:
            self._client = client
        else:
            # Imported here rather than at module scope so that a Redis-backed
            # worker starts without paying boto3's import cost, and so this
            # module can be introspected in an environment without boto3.
            import boto3

            self._client = boto3.client("sqs", region_name=region)

    def send_message(
        self,
        *,
        queue_url: str,
        message_body: str,
        message_attributes: Mapping[str, str] | None = None,
    ) -> None:
        request: dict[str, Any] = {"QueueUrl": queue_url, "MessageBody": message_body}
        if message_attributes:
            request["MessageAttributes"] = {
                key: {"DataType": "String", "StringValue": value}
                for key, value in message_attributes.items()
            }
        self._client.send_message(**request)

    def receive_message(self, *, queue_url: str, wait_time_seconds: int) -> list[SqsMessage]:
        response = self._client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            # Long polling: the worker waits on the queue instead of spinning
            # (Requirement 3.2).
            WaitTimeSeconds=wait_time_seconds,
        )

        messages: list[SqsMessage] = []
        for message in response.get("Messages", []):
            message_id = message.get("MessageId")
            receipt_handle = message.get("ReceiptHandle")
            body = message.get("Body")
            if message_id is None or receipt_handle is None or body is None:
                continue
            messages.append(
                SqsMessage(message_id=message_id, receipt_handle=receipt_handle, body=body)
            )
        return messages

    def delete_message(self, *, queue_url: str, receipt_handle: str) -> None:
        self._client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)

    def approximate_number_of_messages(self, queue_url: str) -> int:
        response = self._client.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=["ApproximateNumberOfMessages"],
        )
        raw = response.get("Attributes", {}).get("ApproximateNumberOfMessages")
        try:
            return int(raw)
        except (TypeError, ValueError):
            # A missing or non-numeric attribute reads as an empty queue rather
            # than crashing the scaler-facing depth check.
            return 0

    def close(self) -> None:
        self._client.close()
