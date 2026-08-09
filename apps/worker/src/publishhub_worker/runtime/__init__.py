"""
Public surface of the worker's runtime: the graceful shutdown mechanism and the
process entrypoint that composes every other package.

The two live together because they are one concern. A stop flag with nothing
installing it is dead code, and an entrypoint that starts consuming without one
can lose an in-flight job the moment Kubernetes scales it away (Requirement 3.6) —
which is why spec tasks 4.2 and 4.3 deliberately left this package for 4.4.
"""

from .entrypoint import (
    EXIT_FAILURE,
    EXIT_OK,
    ClosableRedis,
    RuntimeDeps,
    main,
    run_worker,
)
from .shutdown import (
    CLOSING_ALLOWANCE_SECONDS,
    SHUTDOWN_SIGNALS,
    Closeable,
    ShutdownFlag,
    SignalDeps,
    SignalHandler,
    close_resources,
    install_shutdown_handlers,
    signal_name,
    worst_case_shutdown_seconds,
)

__all__ = [
    "CLOSING_ALLOWANCE_SECONDS",
    "EXIT_FAILURE",
    "EXIT_OK",
    "SHUTDOWN_SIGNALS",
    "ClosableRedis",
    "Closeable",
    "RuntimeDeps",
    "ShutdownFlag",
    "SignalDeps",
    "SignalHandler",
    "close_resources",
    "install_shutdown_handlers",
    "main",
    "run_worker",
    "signal_name",
    "worst_case_shutdown_seconds",
]
