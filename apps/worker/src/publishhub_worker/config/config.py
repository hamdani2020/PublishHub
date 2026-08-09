"""
Startup configuration for the worker (Requirements 5.5, 14.3).

Every variable the worker reads from the design document's configuration
reference is parsed and validated here, once, at startup. Nothing downstream
touches `os.environ`: the job loop receives a `WorkerConfig` whose values are
already the right types and already known to be in range.

Two rules shape this module, the same two that shape `apps/api/src/config/config.ts`:

1. Fail fast, and name the key. A bad value stops the process before it claims a
   message, with a message that says which variable is wrong (Requirement 5.5).
   A worker that dead-letters everything an hour later because
   `MAX_ATTEMPTS=fifteen` silently became a default is a far worse failure mode
   than a refusal to boot.
2. Do not re-derive what the queue layer already owns. Backend selection and its
   per-backend requirements live in `queue/factory.py`; this module calls into it
   and rewraps `QueueConfigError` so callers have a single type to catch.

API-only variables (`PORT`, `NODE_ENV`, `CORS_ORIGINS`) and the web-only
`API_BASE_URL` are deliberately absent: the worker neither reads nor validates
configuration that belongs to another service.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..queue import (
    DEFAULT_AWS_REGION,
    DEFAULT_REDIS_URL,
    SQS_MAX_WAIT_SECONDS,
    Env,
    QueueConfig,
    QueueConfigError,
    resolve_queue_config,
)
from .flags import BOOLEAN_FLAG_HINT, parse_boolean_flag


class ConfigError(Exception):
    """
    Raised when a configuration value is missing or invalid. `key` names the
    offending environment variable, and the message repeats it so a single
    startup log line is actionable on its own (Requirement 5.5).

    `QueueConfigError` raised by the queue factory is rewrapped as this type, so
    a caller starting the worker catches one exception rather than two.
    """

    def __init__(self, key: str, message: str) -> None:
        super().__init__(message)
        self.key = key


#: Local defaults, matching the configuration reference table in design.md.
CONFIG_DEFAULTS: Mapping[str, str] = {
    "REDIS_URL": DEFAULT_REDIS_URL,
    "AWS_REGION": DEFAULT_AWS_REGION,
    "MAX_ATTEMPTS": "3",
    "POLL_WAIT_SECONDS": "20",
    "SIMULATE_LATENCY_MS": "500",
    "SIMULATE_FAILURE_RATE": "0",
    "OBSERVABILITY_ENABLED": "false",
    "DD_SERVICE": "publishhub-worker",
    "DD_ENV": "development",
}

#: Ceilings exist to catch typos, not to express policy. `MAX_ATTEMPTS=300` with
#: exponential backoff turns one poison message into hours of a blocked worker,
#: and a simulated publish longer than a minute reads as a hang rather than as a
#: demo. Both are far above any value this system has a use for.
MAX_ATTEMPTS_CEILING = 10
POLL_WAIT_SECONDS_CEILING = 3600
SIMULATE_LATENCY_MS_CEILING = 60_000

#: `us-east-1`, `eu-west-2`, `ap-southeast-1`, `us-gov-east-1`. Same pattern as
#: the API's configuration module.
AWS_REGION_PATTERN = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")

#: Log levels the worker emits at. Not configurable: derived from `DD_ENV`.
DEVELOPMENT_ENV = "development"
LOG_LEVEL_DEBUG = "debug"
LOG_LEVEL_INFO = "info"


@dataclass(frozen=True, kw_only=True, slots=True)
class ObservabilityConfig:
    """Datadog unified tagging plus the master switch (Requirement 14.6)."""

    #: Master switch for tracing and metric export. Off by default, so local
    #: development needs no Datadog account.
    enabled: bool
    #: `DD_SERVICE`. Also the `service` field on every log line (14.3).
    service: str
    #: `DD_ENV`. Also the `env` field on every log line.
    env: str
    #: `DD_VERSION`. `None` when the build does not stamp one.
    version: str | None


@dataclass(frozen=True, kw_only=True, slots=True)
class SimulationConfig:
    """
    Publishing is simulated, and deliberately so (see the scope boundaries in
    requirements.md). These two knobs are what make the retry and canary paths
    demonstrable without a third-party API.
    """

    #: Per-platform simulated work, milliseconds.
    latency_ms: int
    #: Probability in `[0.0, 1.0]` that a simulated publish fails. `0` disables it.
    failure_rate: float


@dataclass(frozen=True, kw_only=True, slots=True)
class WorkerConfig:
    """Validated startup configuration. The only shape the worker reads."""

    #: Post records live in Redis regardless of which queue backend is active,
    #: so this is validated unconditionally rather than per backend.
    redis_url: str
    aws_region: str
    queue: QueueConfig
    #: Delivery attempts before a job is dead-lettered (Requirement 3.3).
    max_attempts: int
    #: Blocking-receive window. A blocking wait is what keeps idle CPU near zero
    #: instead of spinning (Requirement 3.2).
    poll_wait_seconds: int
    simulation: SimulationConfig
    observability: ObservabilityConfig
    log_level: str


def _read(env: Env, key: str) -> str | None:
    """
    Trimmed value, or `None` when unset or blank. Treating `""` as unset matches
    the queue factory: an empty variable in a container manifest means "not set",
    not "set to the empty string".
    """
    value = env.get(key)
    if value is None:
        return None
    trimmed = value.strip()
    return None if trimmed == "" else trimmed


def _with_defaults(env: Env) -> dict[str, str]:
    """Present-and-non-blank values only, with the local defaults filled in."""
    compacted = {key: _read(env, key) for key in env}
    return {
        **CONFIG_DEFAULTS,
        **{key: value for key, value in compacted.items() if value is not None},
    }


def _require_int(key: str, value: str, *, minimum: int, maximum: int) -> int:
    if re.fullmatch(r"\d+", value) is None:
        raise ConfigError(
            key,
            f"{key} must be a whole number — received {json.dumps(value)}",
        )
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise ConfigError(key, f"{key} must be between {minimum} and {maximum} — received {parsed}")
    return parsed


def _require_rate(key: str, value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise ConfigError(
            key,
            f"{key} must be a number between 0.0 and 1.0 — received {json.dumps(value)}",
        ) from None
    # `float()` accepts "nan" and "inf". A NaN compares false against every
    # bound, so it would slip through the range check below untouched.
    if not math.isfinite(parsed):
        raise ConfigError(
            key,
            f"{key} must be a finite number between 0.0 and 1.0 — received {json.dumps(value)}",
        )
    if parsed < 0.0 or parsed > 1.0:
        raise ConfigError(key, f"{key} must be between 0.0 and 1.0 — received {parsed}")
    return parsed


def _require_boolean(key: str, value: str) -> bool:
    parsed = parse_boolean_flag(value)
    if parsed is None:
        raise ConfigError(key, f"{key} {BOOLEAN_FLAG_HINT} — received {json.dumps(value)}")
    return parsed


def _require_redis_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme == "" or parsed.netloc == "":
        raise ConfigError("REDIS_URL", f"REDIS_URL is not a valid URL: {json.dumps(value)}")
    if parsed.scheme not in ("redis", "rediss"):
        raise ConfigError(
            "REDIS_URL",
            f"REDIS_URL must use redis: or rediss: — received {json.dumps(parsed.scheme + ':')}",
        )
    return value


def _require_region(value: str) -> str:
    if AWS_REGION_PATTERN.fullmatch(value) is None:
        raise ConfigError(
            "AWS_REGION",
            f"AWS_REGION must be an AWS region such as us-east-1 — received {json.dumps(value)}",
        )
    return value


def load_config(env: Env | None = None) -> WorkerConfig:
    """
    Parse and validate the environment. Raises `ConfigError` on the first problem
    found, naming the offending key.
    """
    environment: Env = os.environ if env is None else env
    values = _with_defaults(environment)

    redis_url = _require_redis_url(values["REDIS_URL"])
    aws_region = _require_region(values["AWS_REGION"])
    max_attempts = _require_int(
        "MAX_ATTEMPTS", values["MAX_ATTEMPTS"], minimum=1, maximum=MAX_ATTEMPTS_CEILING
    )
    poll_wait_seconds = _require_int(
        "POLL_WAIT_SECONDS",
        values["POLL_WAIT_SECONDS"],
        minimum=0,
        maximum=POLL_WAIT_SECONDS_CEILING,
    )
    simulation = SimulationConfig(
        latency_ms=_require_int(
            "SIMULATE_LATENCY_MS",
            values["SIMULATE_LATENCY_MS"],
            minimum=0,
            maximum=SIMULATE_LATENCY_MS_CEILING,
        ),
        failure_rate=_require_rate("SIMULATE_FAILURE_RATE", values["SIMULATE_FAILURE_RATE"]),
    )
    observability_enabled = _require_boolean(
        "OBSERVABILITY_ENABLED", values["OBSERVABILITY_ENABLED"]
    )

    try:
        queue = resolve_queue_config(environment)
    except QueueConfigError as error:
        # Same failure, one error type for the caller to catch.
        raise ConfigError(error.key, str(error)) from error

    if queue.backend == "sqs" and poll_wait_seconds > SQS_MAX_WAIT_SECONDS:
        # SQS caps `WaitTimeSeconds` at 20 and the client clamps to it. Silently
        # polling for a fifth of the requested window is the kind of surprise
        # that gets debugged as "KEDA is wrong"; say so at startup instead.
        raise ConfigError(
            "POLL_WAIT_SECONDS",
            f"POLL_WAIT_SECONDS may not exceed {SQS_MAX_WAIT_SECONDS} when QUEUE_BACKEND=sqs — "
            f"received {poll_wait_seconds}",
        )

    env_name = values["DD_ENV"]

    return WorkerConfig(
        redis_url=redis_url,
        aws_region=aws_region,
        queue=queue,
        max_attempts=max_attempts,
        poll_wait_seconds=poll_wait_seconds,
        simulation=simulation,
        observability=ObservabilityConfig(
            enabled=observability_enabled,
            service=values["DD_SERVICE"],
            env=env_name,
            version=values.get("DD_VERSION"),
        ),
        # Development gets debug detail; everywhere else stays at info so poll and
        # heartbeat lines do not bury the events that matter.
        log_level=LOG_LEVEL_DEBUG if env_name == DEVELOPMENT_ENV else LOG_LEVEL_INFO,
    )
