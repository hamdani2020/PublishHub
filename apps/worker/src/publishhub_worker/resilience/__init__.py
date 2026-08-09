"""
Public surface of the worker's resilience primitives: exponential backoff, the
startup connection wait, and the readiness marker.

They live together because they answer one question in two places — what a worker
does when something it depends on is not working yet (Requirements 3.3, 3.7) — and
because keeping the backoff arithmetic out of both callers is what lets each of
them be tested without sleeping.
"""

from .backoff import (
    CONNECT_BASE_SECONDS,
    DEFAULT_BASE_SECONDS,
    DEFAULT_CONNECT_BACKOFF,
    DEFAULT_MAX_SECONDS,
    DEFAULT_MULTIPLIER,
    DEFAULT_RETRY_BACKOFF,
    BackoffPolicy,
)
from .connect import ConnectDeps, ConnectResult, always_continue, wait_for_queue
from .readiness import DEFAULT_READINESS_FILE, ReadinessFile

__all__ = [
    "CONNECT_BASE_SECONDS",
    "DEFAULT_BASE_SECONDS",
    "DEFAULT_CONNECT_BACKOFF",
    "DEFAULT_MAX_SECONDS",
    "DEFAULT_MULTIPLIER",
    "DEFAULT_READINESS_FILE",
    "DEFAULT_RETRY_BACKOFF",
    "BackoffPolicy",
    "ConnectDeps",
    "ConnectResult",
    "ReadinessFile",
    "always_continue",
    "wait_for_queue",
]
