"""
Backend selection (Requirements 5.1, 5.4, 5.5).

Switching between Redis and SQS is an environment-variable change and nothing
else: no business logic branches on the backend. When the selection or its
required settings are wrong, this fails at startup with a message that names the
offending key, rather than surfacing a connection error later from the job loop.

Keys read here match the configuration reference in the design document and the
TypeScript factory exactly: `QUEUE_BACKEND`, `REDIS_URL`, `SQS_QUEUE_URL`,
`AWS_REGION`, plus the optional `SQS_DLQ_URL` for explicit dead-lettering instead
of relying on the queue's redrive policy.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable, Union
from urllib.parse import urlsplit

from .redis_queue_client import RedisCommands, RedisQueueClient, RedisQueueKeys
from .sqs_queue_client import SqsPort, SqsQueueClient
from .types import DeadLetterListener, QueueBackend, QueueClient, QueueConfigError

QUEUE_BACKENDS: tuple[QueueBackend, ...] = ("redis", "sqs")
DEFAULT_QUEUE_BACKEND: QueueBackend = "redis"
DEFAULT_REDIS_URL = "redis://publishhub-redis:6379"
DEFAULT_AWS_REGION = "us-east-1"


@dataclass(frozen=True, slots=True)
class RedisQueueConfig:
    redis_url: str
    backend: QueueBackend = "redis"


@dataclass(frozen=True, slots=True)
class SqsQueueConfig:
    queue_url: str
    dead_letter_queue_url: str | None
    region: str
    backend: QueueBackend = "sqs"


QueueConfig = Union[RedisQueueConfig, SqsQueueConfig]

Env = Mapping[str, str]


@dataclass(frozen=True, slots=True)
class QueueClientDeps:
    """
    Construction seams. Overridable so tests never open a socket, and so the
    worker can pass its own dead-letter logger.
    """

    create_redis: Callable[[str], RedisCommands] | None = None
    create_sqs_port: Callable[[str], SqsPort] | None = None
    on_dead_letter: DeadLetterListener | None = None
    redis_keys: RedisQueueKeys | Mapping[str, str] | None = None


def _read(env: Env, key: str) -> str | None:
    """Trimmed value, or `None` when unset or blank — blank is not an error."""
    value = env.get(key)
    if value is None:
        return None
    trimmed = value.strip()
    return None if trimmed == "" else trimmed


def _require_url(key: str, value: str, schemes: Sequence[str]) -> str:
    parsed = urlsplit(value)
    if parsed.scheme == "" or parsed.netloc == "":
        raise QueueConfigError(key, f"{key} is not a valid URL: {json.dumps(value)}")
    if parsed.scheme not in schemes:
        expected = ", ".join(f"{scheme}:" for scheme in schemes)
        raise QueueConfigError(
            key,
            f"{key} must use one of {expected} — received {json.dumps(parsed.scheme + ':')}",
        )
    return value


def resolve_queue_config(env: Env | None = None) -> QueueConfig:
    """
    Pure configuration resolution. Raises `QueueConfigError` carrying the
    offending key, so startup validation and tests can assert on the key rather
    than on message wording.
    """
    environment: Env = os.environ if env is None else env

    requested = (_read(environment, "QUEUE_BACKEND") or DEFAULT_QUEUE_BACKEND).lower()
    if requested not in QUEUE_BACKENDS:
        allowed = ", ".join(QUEUE_BACKENDS)
        raise QueueConfigError(
            "QUEUE_BACKEND",
            f"QUEUE_BACKEND must be one of {allowed} — received {json.dumps(requested)}",
        )

    if requested == "redis":
        redis_url = _read(environment, "REDIS_URL") or DEFAULT_REDIS_URL
        return RedisQueueConfig(
            redis_url=_require_url("REDIS_URL", redis_url, ("redis", "rediss")),
        )

    queue_url = _read(environment, "SQS_QUEUE_URL")
    if queue_url is None:
        raise QueueConfigError(
            "SQS_QUEUE_URL",
            "SQS_QUEUE_URL is required when QUEUE_BACKEND=sqs",
        )

    dead_letter_queue_url = _read(environment, "SQS_DLQ_URL")

    return SqsQueueConfig(
        queue_url=_require_url("SQS_QUEUE_URL", queue_url, ("https", "http")),
        dead_letter_queue_url=(
            None
            if dead_letter_queue_url is None
            else _require_url("SQS_DLQ_URL", dead_letter_queue_url, ("https", "http"))
        ),
        region=_read(environment, "AWS_REGION") or DEFAULT_AWS_REGION,
    )


def _default_redis(redis_url: str) -> RedisCommands:
    # Imported here so that an SQS-backed worker does not need redis-py loaded,
    # and so this module imports in an environment without it.
    import redis

    return redis.Redis.from_url(
        redis_url,
        # The client works in `str`, not `bytes`: payloads are JSON text and the
        # processing-list member has to compare equal to what `LREM` is given.
        decode_responses=True,
        # A blocking receive must not be cut short by a socket read timeout.
        socket_timeout=None,
    )


def _default_sqs_port(region: str) -> SqsPort:
    from .aws_sqs_port import AwsSqsPort

    return AwsSqsPort(region=region)


def create_queue_client_from_config(
    config: QueueConfig,
    deps: QueueClientDeps | None = None,
) -> QueueClient:
    """Build the client for the configured backend."""
    dependencies = QueueClientDeps() if deps is None else deps

    if isinstance(config, RedisQueueConfig):
        create_redis = dependencies.create_redis or _default_redis
        return RedisQueueClient(
            create_redis(config.redis_url),
            keys=dependencies.redis_keys,
            on_dead_letter=dependencies.on_dead_letter,
        )

    create_sqs_port = dependencies.create_sqs_port or _default_sqs_port
    return SqsQueueClient(
        create_sqs_port(config.region),
        queue_url=config.queue_url,
        dead_letter_queue_url=config.dead_letter_queue_url,
        on_dead_letter=dependencies.on_dead_letter,
    )


def create_queue_client(
    env: Env | None = None,
    deps: QueueClientDeps | None = None,
) -> QueueClient:
    """Resolve configuration from the environment, then build the client."""
    return create_queue_client_from_config(resolve_queue_config(env), deps)
