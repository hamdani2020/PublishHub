"""
SQS backend unit tests against a fake port (Requirement 5.3).

Same observable behavior as the Redis backend, different primitives: delete
instead of list removal, long polling instead of a blocking pop, and a redrive
policy as the default dead-letter path.

Mirrors `apps/api/src/queue/sqs-queue-client.test.ts`.
"""

from __future__ import annotations

import json

import pytest

from publishhub_worker.queue.publish_job import create_publish_job, serialize_publish_job
from publishhub_worker.queue.sqs_queue_client import SQS_MAX_WAIT_SECONDS, SqsQueueClient
from publishhub_worker.queue.testing import FakeSqsPort
from publishhub_worker.queue.testing.fake_sqs_port import DeletedMessage, SentMessage
from publishhub_worker.queue.types import (
    DeadLetterEvent,
    PublishJob,
    ReceivedJob,
    RedisJobHandle,
)

QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/publishhub-jobs"
DLQ_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/publishhub-jobs-dlq"


def make_job(**overrides) -> PublishJob:
    arguments = {
        "post_id": "post_01HZX3QK7M9V4TDR8N2C5EAB6F",
        "content": "hello",
        "platforms": ("twitter",),
    }
    arguments.update(overrides)
    return create_publish_job(**arguments)


@pytest.fixture()
def port() -> FakeSqsPort:
    return FakeSqsPort(default_queue_url=QUEUE_URL)


@pytest.fixture()
def dead_lettered() -> list[DeadLetterEvent]:
    return []


@pytest.fixture()
def client(port: FakeSqsPort, dead_lettered: list[DeadLetterEvent]) -> SqsQueueClient:
    return SqsQueueClient(port, queue_url=QUEUE_URL, on_dead_letter=dead_lettered.append)


def test_sends_the_same_json_body_the_redis_backend_would_store(
    client: SqsQueueClient, port: FakeSqsPort
) -> None:
    job = make_job()

    client.enqueue(job)

    assert port.sent == [
        SentMessage(queue_url=QUEUE_URL, message_body=serialize_publish_job(job))
    ]


def test_long_polls_with_the_requested_wait_and_returns_the_parsed_job(
    client: SqsQueueClient, port: FakeSqsPort
) -> None:
    job = make_job()
    port.seed(serialize_publish_job(job))

    received = client.receive(20)

    assert [call.wait_time_seconds for call in port.receive_calls] == [20]
    assert received is not None
    assert received.job == job
    assert received.handle.backend == "sqs"
    assert received.handle.message_id == "message-1"
    assert received.handle.receipt_handle == "receipt-1"


def test_clamps_the_wait_to_the_sqs_maximum_and_floors_it_at_zero(
    client: SqsQueueClient, port: FakeSqsPort
) -> None:
    client.receive(120)
    client.receive(-1)

    assert [call.wait_time_seconds for call in port.receive_calls] == [SQS_MAX_WAIT_SECONDS, 0]


def test_returns_none_when_the_long_poll_finds_nothing(client: SqsQueueClient) -> None:
    assert client.receive(1) is None


def test_deletes_the_message_on_ack(client: SqsQueueClient, port: FakeSqsPort) -> None:
    port.seed(serialize_publish_job(make_job()))
    received = client.receive(20)
    assert received is not None

    client.ack(received)

    assert port.deleted == [DeletedMessage(queue_url=QUEUE_URL, receipt_handle="receipt-1")]


def test_leaves_the_message_for_the_redrive_policy_when_no_dlq_url_is_configured(
    client: SqsQueueClient, port: FakeSqsPort, dead_lettered: list[DeadLetterEvent]
) -> None:
    port.seed(serialize_publish_job(make_job()))
    received = client.receive(20)
    assert received is not None

    client.dead_letter(received, "max_attempts_exhausted")

    # Deleting here would discard the message instead of dead-lettering it: the
    # queue's redrive policy is what moves it after maxReceiveCount.
    assert port.deleted == []
    assert port.sent == []
    assert dead_lettered[0].via_redrive_policy is True


def test_sends_to_the_dlq_and_deletes_from_the_main_queue_when_a_dlq_url_is_configured(
    port: FakeSqsPort, dead_lettered: list[DeadLetterEvent]
) -> None:
    explicit = SqsQueueClient(
        port,
        queue_url=QUEUE_URL,
        dead_letter_queue_url=DLQ_URL,
        on_dead_letter=dead_lettered.append,
    )
    job = make_job()
    port.seed(serialize_publish_job(job))
    received = explicit.receive(20)
    assert received is not None

    explicit.dead_letter(received, "schema_validation_failed")

    assert port.sent == [
        SentMessage(
            queue_url=DLQ_URL,
            # Body unchanged, so the dead letter can be replayed into either backend.
            message_body=serialize_publish_job(job),
            message_attributes={"DeadLetterReason": "schema_validation_failed"},
        )
    ]
    assert port.deleted == [DeletedMessage(queue_url=QUEUE_URL, receipt_handle="receipt-1")]
    assert dead_lettered[0] == DeadLetterEvent(
        backend="sqs",
        reason="schema_validation_failed",
        job_id=job.job_id,
        post_id=job.post_id,
        attempt=1,
        via_redrive_policy=False,
    )


def test_surfaces_an_unparseable_body_instead_of_raising(
    client: SqsQueueClient, port: FakeSqsPort
) -> None:
    port.seed('["twitter", "linkedin"]')

    received = client.receive(20)

    assert received is not None
    assert received.job is None
    assert received.invalid_reason == "unparseable_payload"
    assert received.raw == '["twitter", "linkedin"]'


def test_reports_an_unknown_schema_version_as_such(
    client: SqsQueueClient, port: FakeSqsPort
) -> None:
    payload = json.loads(serialize_publish_job(make_job()))
    payload["schema_version"] = 2
    port.seed(json.dumps(payload))

    received = client.receive(20)

    assert received is not None
    assert received.invalid_reason == "unknown_schema_version"


def test_reports_approximate_queue_depth_the_number_keda_scales_on(
    client: SqsQueueClient, port: FakeSqsPort
) -> None:
    port.depth_value = 42

    assert client.depth() == 42


def test_rejects_a_handle_from_the_other_backend_rather_than_deleting_the_wrong_message(
    client: SqsQueueClient,
) -> None:
    foreign = ReceivedJob(raw="{}", handle=RedisJobHandle(payload="{}"))

    with pytest.raises(TypeError, match="not portable across backends"):
        client.ack(foreign)


def test_closes_the_underlying_client(client: SqsQueueClient, port: FakeSqsPort) -> None:
    client.close()

    assert port.closed is True
