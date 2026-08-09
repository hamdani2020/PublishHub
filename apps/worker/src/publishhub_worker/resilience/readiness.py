"""
Readiness reporting for a process with no HTTP server (Requirement 3.7).

The API answers readiness on `/ready`. The worker has no listener — it is a
consumer, nothing calls it — so its readiness signal is the presence of a file,
which a Kubernetes `readinessProbe` reads with `exec`:

```yaml
readinessProbe:
  exec:
    command: ["test", "-f", "/tmp/publishhub-worker.ready"]
```

The chart wires that probe in spec task 9.2. What matters here is the invariant
the design states for both services: liveness reflects the process, readiness
reflects dependencies. A worker whose queue is unreachable is a live process that
is not ready, so it keeps running, keeps retrying, and keeps saying "not ready" —
rather than exiting and being restarted every few seconds, which is the
crash-looping Requirement 3.7 rules out.

The path is a constant rather than an environment variable on purpose: the
configuration reference in the design document is the contract for what the
worker reads from its environment, and a probe command and a marker file that
have to agree are better expressed together in the chart than as one more
variable to keep in sync. `/tmp` is the one directory a hardened, read-only-
friendly container image is expected to have writable (an `emptyDir`), which is
why the default lives there and not next to the code.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Default marker path. Must match the chart's readiness probe command.
DEFAULT_READINESS_FILE = "/tmp/publishhub-worker.ready"


class ReadinessFile:
    """
    Creates and removes the readiness marker.

    Both operations are idempotent: marking ready twice writes the file twice,
    marking unready when the file is already gone does nothing. Callers are
    startup paths and signal handlers, and neither is a good place for a
    conditional.
    """

    __slots__ = ("_path",)

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_READINESS_FILE) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def ready(self) -> bool:
        """Whether the marker currently exists, which is what the probe tests."""
        return self._path.exists()

    def mark_ready(self) -> None:
        """
        Create the marker. The contents are informational — a probe that runs
        `test -f` never reads them — so it holds the reason rather than nothing,
        because a developer who does `cat` the file deserves an answer.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("queue reachable\n", encoding="utf-8")

    def mark_unready(self) -> None:
        """Remove the marker. Absent is the same as unready, so a missing file is fine."""
        self._path.unlink(missing_ok=True)
