"""
`python -m publishhub_worker` — how the container starts the worker.

Two lines, and both are deliberate. The exit code comes from `main` rather than
from an uncaught exception, so a bad configuration variable exits 1 with one
readable line (Requirement 5.5) and a graceful stop exits 0 (Requirement 3.6).
And `SystemExit` is raised rather than `sys.exit` called, which is the same thing
in a script and the honest thing in a module.

The image's `CMD` should be the module form, not a path to a file: `python -m`
makes the process PID 1 for the container with no shell in between, so `SIGTERM`
from Kubernetes reaches the handler installed in `runtime/entrypoint.py` instead
of a shell that would not forward it (spec task 7.1).
"""

from .runtime import main

if __name__ == "__main__":
    raise SystemExit(main())
