"""
Tracer initialization, as a module side effect.

This is the one place in the worker where doing work at import time is the correct
design rather than a smell. `ddtrace` patches `redis` and `botocore` when it loads,
and it can only patch a module that has not been evaluated yet — so the only way to
get in front of them is to run during the first import of the process entrypoint.

Which is why `runtime/entrypoint.py` imports this module first, and why nothing else
imports it at all: importing it is what starts the tracer. `observability/__init__.py`
deliberately does not re-export it, so a test that reaches for the module's public
surface cannot accidentally load an APM library.

With `OBSERVABILITY_ENABLED` unset or false, evaluating this module reads four
environment variables and returns `INERT_TRACING`. Nothing is loaded, nothing is
patched, no socket is opened (Requirement 14.6).
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace

from .ddtrace_loader import load_ddtrace_port
from .tracing import Tracing, create_tracing, tracing_options_from_env


def _report_load_error(error: BaseException) -> None:
    """
    A failed load goes to stderr and is then forgotten. There is no logger this
    early — its `service` and `env` fields come from a configuration that has not
    been parsed yet — and a missing APM library is not a reason to refuse to
    process jobs.
    """
    sys.stderr.write(f"tracing disabled: ddtrace failed to initialize: {error}\n")


#: The process-wide tracing seam. Inert unless the switch is on.
tracing: Tracing = create_tracing(
    replace(tracing_options_from_env(os.environ), on_load_error=_report_load_error),
    load_ddtrace_port,
)
