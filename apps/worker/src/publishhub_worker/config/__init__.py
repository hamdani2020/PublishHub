"""
Public surface of the worker's configuration module.

Mirrors `apps/api/src/config/index.ts`.
"""

from .config import (
    AWS_REGION_PATTERN,
    CONFIG_DEFAULTS,
    DEVELOPMENT_ENV,
    LOG_LEVEL_DEBUG,
    LOG_LEVEL_INFO,
    MAX_ATTEMPTS_CEILING,
    POLL_WAIT_SECONDS_CEILING,
    SIMULATE_LATENCY_MS_CEILING,
    ConfigError,
    ObservabilityConfig,
    SimulationConfig,
    WorkerConfig,
    load_config,
)
from .flags import (
    BOOLEAN_FLAG_HINT,
    FALSY_FLAG_VALUES,
    TRUTHY_FLAG_VALUES,
    parse_boolean_flag,
)

__all__ = [
    "AWS_REGION_PATTERN",
    "BOOLEAN_FLAG_HINT",
    "CONFIG_DEFAULTS",
    "DEVELOPMENT_ENV",
    "FALSY_FLAG_VALUES",
    "LOG_LEVEL_DEBUG",
    "LOG_LEVEL_INFO",
    "MAX_ATTEMPTS_CEILING",
    "POLL_WAIT_SECONDS_CEILING",
    "SIMULATE_LATENCY_MS_CEILING",
    "TRUTHY_FLAG_VALUES",
    "ConfigError",
    "ObservabilityConfig",
    "SimulationConfig",
    "WorkerConfig",
    "load_config",
    "parse_boolean_flag",
]
