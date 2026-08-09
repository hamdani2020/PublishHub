"""
Public surface of the worker's logging module.

Mirrors `apps/api/src/logging/index.ts`. Note that this package shadows the
stdlib `logging` name only for code that imports it explicitly as
`publishhub_worker.logging`; a plain `import logging` anywhere in this package
still resolves to the standard library, because Python 3 imports are absolute.
"""

from .logger import (
    FIELDS_ATTRIBUTE,
    LEVEL_LABELS,
    LEVEL_NUMBERS,
    RESERVED_FIELDS,
    SILENT_LEVEL,
    JsonFormatter,
    LoggerDeps,
    TraceContextProvider,
    WorkerLogger,
    create_logger,
    format_timestamp,
    resolve_level,
)

__all__ = [
    "FIELDS_ATTRIBUTE",
    "LEVEL_LABELS",
    "LEVEL_NUMBERS",
    "RESERVED_FIELDS",
    "SILENT_LEVEL",
    "JsonFormatter",
    "LoggerDeps",
    "TraceContextProvider",
    "WorkerLogger",
    "create_logger",
    "format_timestamp",
    "resolve_level",
]
