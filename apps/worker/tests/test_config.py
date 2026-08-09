"""
Worker configuration tests (Requirements 5.5, 14.3).

The behavior under test is narrow and important: a value that is wrong stops the
worker before it claims a message, and the failure names the environment variable
that is wrong rather than describing a symptom.

Mirrors `apps/api/src/config/testing/config.test.ts` in shape, so a divergence
between the two services' configuration handling shows up as a failing test in
one language.
"""

from __future__ import annotations

import pytest

from publishhub_worker.config import (
    CONFIG_DEFAULTS,
    LOG_LEVEL_DEBUG,
    LOG_LEVEL_INFO,
    MAX_ATTEMPTS_CEILING,
    ConfigError,
    load_config,
)
from publishhub_worker.queue import (
    DEFAULT_AWS_REGION,
    DEFAULT_REDIS_URL,
    RedisQueueConfig,
    SqsQueueConfig,
)

QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/publishhub-jobs"


def expect_config_error(env: dict[str, str], key: str) -> ConfigError:
    with pytest.raises(ConfigError) as raised:
        load_config(env)

    error = raised.value
    assert error.key == key
    # The message names the offending key, so a single startup line is actionable.
    assert key in str(error)
    return error


# --- defaults -----------------------------------------------------------------


def test_runs_on_local_defaults_with_an_empty_environment() -> None:
    config = load_config({})

    assert config.redis_url == DEFAULT_REDIS_URL
    assert config.aws_region == DEFAULT_AWS_REGION
    assert config.queue == RedisQueueConfig(redis_url=DEFAULT_REDIS_URL)
    assert config.max_attempts == 3
    assert config.poll_wait_seconds == 20
    assert config.simulation.latency_ms == 500
    assert config.simulation.failure_rate == 0.0
    # Off by default, so local development needs no Datadog account (14.6).
    assert config.observability.enabled is False
    assert config.observability.service == "publishhub-worker"
    assert config.observability.env == "development"
    assert config.observability.version is None
    assert config.log_level == LOG_LEVEL_DEBUG


def test_defaults_table_matches_the_documented_configuration_reference() -> None:
    # design.md is the source of truth for these; drift here means the docs lie.
    assert dict(CONFIG_DEFAULTS) == {
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


def test_treats_blank_values_as_unset_rather_than_as_errors() -> None:
    config = load_config({"MAX_ATTEMPTS": "   ", "DD_SERVICE": "", "SIMULATE_FAILURE_RATE": " "})

    assert config.max_attempts == 3
    assert config.observability.service == "publishhub-worker"
    assert config.simulation.failure_rate == 0.0


# --- parsing ------------------------------------------------------------------


def test_parses_every_worker_variable_into_its_own_type() -> None:
    config = load_config(
        {
            "REDIS_URL": "rediss://cache.example:6380",
            "AWS_REGION": "eu-west-1",
            "MAX_ATTEMPTS": " 5 ",
            "POLL_WAIT_SECONDS": "10",
            "SIMULATE_LATENCY_MS": "0",
            "SIMULATE_FAILURE_RATE": "0.25",
            "OBSERVABILITY_ENABLED": "TRUE",
            "DD_SERVICE": "publishhub-worker-canary",
            "DD_ENV": "production",
            "DD_VERSION": "1.4.2",
        }
    )

    assert config.redis_url == "rediss://cache.example:6380"
    assert config.aws_region == "eu-west-1"
    assert config.max_attempts == 5
    assert config.poll_wait_seconds == 10
    assert config.simulation.latency_ms == 0
    assert config.simulation.failure_rate == 0.25
    assert config.observability.enabled is True
    assert config.observability.service == "publishhub-worker-canary"
    assert config.observability.env == "production"
    assert config.observability.version == "1.4.2"
    # Outside development, info keeps poll and heartbeat noise out of the way.
    assert config.log_level == LOG_LEVEL_INFO


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "ON", " True "])
def test_accepts_every_documented_spelling_of_a_true_flag(value: str) -> None:
    assert load_config({"OBSERVABILITY_ENABLED": value}).observability.enabled is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", " False "])
def test_accepts_every_documented_spelling_of_a_false_flag(value: str) -> None:
    assert load_config({"OBSERVABILITY_ENABLED": value}).observability.enabled is False


def test_carries_the_selected_queue_backend_through_untouched() -> None:
    config = load_config({"QUEUE_BACKEND": "sqs", "SQS_QUEUE_URL": QUEUE_URL})

    assert config.queue == SqsQueueConfig(
        queue_url=QUEUE_URL,
        dead_letter_queue_url=None,
        region=DEFAULT_AWS_REGION,
    )
    # Post records live in Redis even when jobs go to SQS, so its URL is still
    # resolved rather than left unset.
    assert config.redis_url == DEFAULT_REDIS_URL


def test_reads_the_process_environment_only_when_no_environment_is_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_ATTEMPTS", "9")

    assert load_config().max_attempts == 9
    # Guards against `os.environ` leaking into a caller that passed its own map.
    assert load_config({}).max_attempts == 3


# --- fail fast, naming the key ------------------------------------------------


@pytest.mark.parametrize(
    ("env", "key"),
    [
        ({"REDIS_URL": "not-a-url"}, "REDIS_URL"),
        ({"REDIS_URL": "http://localhost:6379"}, "REDIS_URL"),
        ({"AWS_REGION": "useast1"}, "AWS_REGION"),
        ({"MAX_ATTEMPTS": "three"}, "MAX_ATTEMPTS"),
        ({"MAX_ATTEMPTS": "0"}, "MAX_ATTEMPTS"),
        ({"MAX_ATTEMPTS": str(MAX_ATTEMPTS_CEILING + 1)}, "MAX_ATTEMPTS"),
        ({"MAX_ATTEMPTS": "-1"}, "MAX_ATTEMPTS"),
        ({"MAX_ATTEMPTS": "3.5"}, "MAX_ATTEMPTS"),
        ({"POLL_WAIT_SECONDS": "-5"}, "POLL_WAIT_SECONDS"),
        ({"POLL_WAIT_SECONDS": "999999"}, "POLL_WAIT_SECONDS"),
        ({"SIMULATE_LATENCY_MS": "half a second"}, "SIMULATE_LATENCY_MS"),
        ({"SIMULATE_LATENCY_MS": "600000"}, "SIMULATE_LATENCY_MS"),
        ({"SIMULATE_FAILURE_RATE": "sometimes"}, "SIMULATE_FAILURE_RATE"),
        ({"SIMULATE_FAILURE_RATE": "1.5"}, "SIMULATE_FAILURE_RATE"),
        ({"SIMULATE_FAILURE_RATE": "-0.1"}, "SIMULATE_FAILURE_RATE"),
        ({"SIMULATE_FAILURE_RATE": "nan"}, "SIMULATE_FAILURE_RATE"),
        ({"SIMULATE_FAILURE_RATE": "inf"}, "SIMULATE_FAILURE_RATE"),
        ({"OBSERVABILITY_ENABLED": "maybe"}, "OBSERVABILITY_ENABLED"),
    ],
)
def test_fails_fast_naming_the_key_when_a_value_is_invalid(env: dict[str, str], key: str) -> None:
    expect_config_error(env, key)


def test_reports_queue_configuration_failures_as_config_errors() -> None:
    # The queue factory raises its own error type; callers should only need to
    # catch one, and the offending key has to survive the rewrap.
    expect_config_error({"QUEUE_BACKEND": "kafka"}, "QUEUE_BACKEND")
    expect_config_error({"QUEUE_BACKEND": "sqs"}, "SQS_QUEUE_URL")
    expect_config_error(
        {"QUEUE_BACKEND": "sqs", "SQS_QUEUE_URL": QUEUE_URL, "SQS_DLQ_URL": "nope"},
        "SQS_DLQ_URL",
    )


def test_rejects_a_poll_window_sqs_would_silently_clamp() -> None:
    # Valid for a blocking Redis receive, impossible for SQS long polling.
    assert load_config({"POLL_WAIT_SECONDS": "45"}).poll_wait_seconds == 45

    error = expect_config_error(
        {"QUEUE_BACKEND": "sqs", "SQS_QUEUE_URL": QUEUE_URL, "POLL_WAIT_SECONDS": "45"},
        "POLL_WAIT_SECONDS",
    )
    assert "sqs" in str(error)


def test_error_message_shows_the_offending_value() -> None:
    error = expect_config_error({"MAX_ATTEMPTS": "three"}, "MAX_ATTEMPTS")

    assert "three" in str(error)
