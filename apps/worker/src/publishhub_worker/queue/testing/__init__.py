"""
Test doubles and fixture loading for the queue abstraction.

Shipped inside the package rather than under `tests/` so that later suites — the
job loop in spec task 4.2, the integration test in 6.2 — reuse the same fakes
instead of writing their own.
"""

from .fake_redis import FakeRedis
from .fake_sqs_port import DeletedMessage, FakeSqsPort, ReceiveCall, SentMessage
from .fixture import FIXTURE_PATH, REPO_ROOT, load_publish_job_fixture, serialize_fixture_message

__all__ = [
    "FIXTURE_PATH",
    "REPO_ROOT",
    "DeletedMessage",
    "FakeRedis",
    "FakeSqsPort",
    "ReceiveCall",
    "SentMessage",
    "load_publish_job_fixture",
    "serialize_fixture_message",
]
