"""
Factory and configuration tests (Requirements 5.1, 5.4, 5.5).

Two behaviors matter here: switching backends is an environment change and nothing
else, and a bad or missing setting fails immediately with the name of the key that
is wrong.

Mirrors `apps/api/src/queue/factory.test.ts`, including the same environment
matrix, so a divergence between the two factories shows up as a failing test in
one language.
"""

from __future__ import annotations

import pytest

from publishhub_worker.queue.factory import (
    DEFAULT_AWS_REGION,
    DEFAULT_REDIS_URL,
    QueueClientDeps,
    RedisQueueConfig,
    SqsQueueConfig,
    create_queue_client,
    resolve_queue_config,
)
from publishhub_worker.queue.redis_queue_client import RedisQueueClient
from publishhub_worker.queue.sqs_queue_client import SqsQueueClient
from publishhub_worker.queue.testing import FakeRedis, FakeSqsPort
from publishhub_worker.queue.types import PublishJob, QueueConfigError

QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/publishhub-jobs"
DLQ_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/publishhub-jobs-dlq"


def fakes(**overrides) -> QueueClientDeps:
    return QueueClientDeps(
        create_redis=lambda _redis_url: FakeRedis(),
        create_sqs_port=lambda _region: FakeSqsPort(default_queue_url=QUEUE_URL),
        **overrides,
    )


def expect_config_error(env: dict[str, str], key: str) -> QueueConfigError:
    with pytest.raises(QueueConfigError) as raised:
        resolve_queue_config(env)

    error = raised.value
    assert error.key == key
    # The message names the offending key, so the failure is actionable.
    assert key in str(error)
    return error


# --- resolve_queue_config -----------------------------------------------------


def test_defaults_to_the_redis_backend_for_local_development() -> None:
    assert resolve_queue_config({}) == RedisQueueConfig(redis_url=DEFAULT_REDIS_URL)


def test_ignores_case_and_surrounding_whitespace_in_queue_backend() -> None:
    config = resolve_queue_config({"QUEUE_BACKEND": "  SQS ", "SQS_QUEUE_URL": QUEUE_URL})

    assert config == SqsQueueConfig(
        queue_url=QUEUE_URL,
        dead_letter_queue_url=None,
        region=DEFAULT_AWS_REGION,
    )


def test_treats_an_empty_value_as_unset_rather_than_as_an_error() -> None:
    config = resolve_queue_config({"QUEUE_BACKEND": "", "REDIS_URL": "   "})

    assert config == RedisQueueConfig(redis_url=DEFAULT_REDIS_URL)


def test_accepts_an_explicit_redis_url_including_tls() -> None:
    config = resolve_queue_config({"REDIS_URL": "rediss://cache.example:6380"})

    assert config == RedisQueueConfig(redis_url="rediss://cache.example:6380")


def test_reads_the_optional_sqs_dead_letter_queue_url_and_region() -> None:
    config = resolve_queue_config(
        {
            "QUEUE_BACKEND": "sqs",
            "SQS_QUEUE_URL": QUEUE_URL,
            "SQS_DLQ_URL": DLQ_URL,
            "AWS_REGION": "eu-west-1",
        }
    )

    assert config == SqsQueueConfig(
        queue_url=QUEUE_URL,
        dead_letter_queue_url=DLQ_URL,
        region="eu-west-1",
    )


def test_fails_fast_naming_queue_backend_when_the_backend_is_unknown() -> None:
    error = expect_config_error({"QUEUE_BACKEND": "kafka"}, "QUEUE_BACKEND")

    assert "redis, sqs" in str(error)


def test_fails_fast_naming_sqs_queue_url_when_it_is_missing_for_the_sqs_backend() -> None:
    expect_config_error({"QUEUE_BACKEND": "sqs"}, "SQS_QUEUE_URL")
    expect_config_error({"QUEUE_BACKEND": "sqs", "SQS_QUEUE_URL": "   "}, "SQS_QUEUE_URL")


@pytest.mark.parametrize(
    ("env", "key"),
    [
        ({"REDIS_URL": "not-a-url"}, "REDIS_URL"),
        ({"REDIS_URL": "http://localhost:6379"}, "REDIS_URL"),
        ({"QUEUE_BACKEND": "sqs", "SQS_QUEUE_URL": "sqs-queue"}, "SQS_QUEUE_URL"),
        (
            {"QUEUE_BACKEND": "sqs", "SQS_QUEUE_URL": QUEUE_URL, "SQS_DLQ_URL": "nope"},
            "SQS_DLQ_URL",
        ),
    ],
)
def test_fails_fast_naming_the_key_when_a_url_is_malformed(env: dict[str, str], key: str) -> None:
    expect_config_error(env, key)


# --- create_queue_client ------------------------------------------------------


def test_builds_the_redis_client_when_the_backend_is_redis() -> None:
    client = create_queue_client({"QUEUE_BACKEND": "redis"}, fakes())

    assert isinstance(client, RedisQueueClient)


def test_builds_the_sqs_client_when_the_backend_is_sqs() -> None:
    client = create_queue_client(
        {"QUEUE_BACKEND": "sqs", "SQS_QUEUE_URL": QUEUE_URL}, fakes()
    )

    assert isinstance(client, SqsQueueClient)


@pytest.mark.parametrize(
    "env",
    [
        {"QUEUE_BACKEND": "redis"},
        {"QUEUE_BACKEND": "sqs", "SQS_QUEUE_URL": QUEUE_URL},
    ],
    ids=["redis", "sqs"],
)
def test_exposes_one_interface_so_callers_never_branch_on_the_backend(
    env: dict[str, str],
) -> None:
    job = PublishJob(
        job_id="3f2a9b0c-5d41-4e8b-9c2a-7d6e5f4a3b21",
        post_id="post_01HZX3QK7M9V4TDR8N2C5EAB6F",
        content="same code path either way",
        platforms=("twitter",),
        attempt=1,
        enqueued_at="2026-08-07T10:00:00.000Z",
        trace_context={},
    )
    client = create_queue_client(env, fakes())

    client.enqueue(job)
    received = client.receive(0)

    assert received is not None
    assert received.job == job
    client.ack(received)
    client.close()


def test_propagates_the_dead_letter_listener_to_the_selected_backend() -> None:
    events: list[str] = []
    client = create_queue_client(
        {"QUEUE_BACKEND": "redis"},
        fakes(on_dead_letter=lambda event: events.append(f"{event.backend}:{event.reason}")),
    )
    job = PublishJob(
        job_id="3f2a9b0c-5d41-4e8b-9c2a-7d6e5f4a3b21",
        post_id="post_01HZX3QK7M9V4TDR8N2C5EAB6F",
        content="doomed",
        platforms=("twitter",),
        attempt=3,
        enqueued_at="2026-08-07T10:00:00.000Z",
        trace_context={},
    )

    client.enqueue(job)
    received = client.receive(0)
    assert received is not None
    client.dead_letter(received, "max_attempts_exhausted")

    assert events == ["redis:max_attempts_exhausted"]


def test_does_not_read_the_environment_when_configuration_is_passed_explicitly() -> None:
    # Guards against `os.environ` leaking into a test run through the default.
    with pytest.raises(QueueConfigError):
        resolve_queue_config({"QUEUE_BACKEND": "sqs"})
