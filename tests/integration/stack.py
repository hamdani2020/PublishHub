"""
Driving the docker compose stack from a test (spec task 6.2).

This module is the plumbing — starting the stack, talking to the API over HTTP,
reading Redis through `redis-cli` — so that `test_end_to_end.py` reads as the two
assertions the task asks for and nothing else.

Three deliberate choices worth knowing before reading further.

**The standard library only.** The suite's one dependency is pytest. HTTP goes
through `urllib.request` and Redis through `docker compose exec redis redis-cli`,
which means the integration suite needs neither `requests` nor `redis-py` and
cannot drift from the stack by pinning a client version the services do not use.
Reading the queue with the same CLI an operator would use also keeps the assertion
honest: it observes the list the design names (`publishhub:jobs:dlq`), not a
Python object the test constructed.

**The same compose file `make dev-up` uses, not a copy.** A second compose
definition for tests would be a second thing to keep true, and the point of an
integration test here is to exercise the environment developers actually run. The
consequence is that the suite shares the `publishhub` project name and its
published ports, so it detects an already-running stack and reuses it rather than
fighting it (see `ComposeStack.ensure_up`).

**Subprocesses take argument lists.** No `shell=True` anywhere, so a service name
or a Redis key cannot turn into a shell command.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yaml"

#: Where the compose stack publishes the API. Bound to loopback in
#: docker-compose.yaml because the API is unauthenticated by design
#: (Requirement 2.9), which is also why the test reaches it at 127.0.0.1 rather
#: than at a host name.
DEFAULT_API_BASE_URL = "http://127.0.0.1:8080"

#: Long enough to cover a clean checkout: the compose services run `npm install`
#: and `pip install` at start, and the first run is minutes where later runs are
#: seconds. Overridable for a slower machine or a warmer cache.
DEFAULT_STARTUP_TIMEOUT_SECONDS = 420.0

#: How long one submitted post may take to reach a terminal status. Generous
#: against the stack's simulated latency and one retry backoff, and still short
#: enough that a genuinely stuck worker fails the test rather than hanging it.
DEFAULT_JOB_TIMEOUT_SECONDS = 90.0

#: The services this suite needs running. `web` is deliberately absent: the path
#: under test is browser-independent — a request to the API, a job through Redis,
#: a worker — and the frontend's own suite covers the rest.
TESTED_SERVICES: tuple[str, ...] = ("redis", "api", "worker")

#: Baseline worker simulation for the suite: fast, and never failing. The
#: dead-letter test overrides both values for its own worker.
BASELINE_WORKER_ENV: Mapping[str, str] = {
    "SIMULATE_LATENCY_MS": "50",
    "SIMULATE_FAILURE_RATE": "0",
    "MAX_ATTEMPTS": "3",
}

#: Statuses that end a post's life, matching `TERMINAL_POST_STATUSES` in
#: apps/worker/src/publishhub_worker/posts/post_store.py. Repeated rather than
#: imported: the integration suite asserts against the API's JSON, so it should
#: not depend on either service's source tree.
TERMINAL_POST_STATUSES = frozenset({"published", "partially_published", "failed"})

T = TypeVar("T")


class StackError(RuntimeError):
    """A compose command failed. Carries the command output for the report."""


def docker_unavailable_reason() -> str | None:
    """
    Why this suite cannot run, or `None` when it can.

    Returned as a message rather than raised because every one of these
    conditions is a *skip*, not a failure: a machine without Docker has not
    broken PublishHub, and `make test` has to stay green on it. The message names
    the fix in each case, so a skipped run is still actionable.
    """
    if os.environ.get("PUBLISHHUB_SKIP_INTEGRATION", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return "PUBLISHHUB_SKIP_INTEGRATION is set — integration suite skipped on request"

    if shutil.which("docker") is None:
        return (
            "docker is not installed or not on PATH — install Docker Desktop "
            "(brew install --cask docker) to run the integration suite"
        )

    if not COMPOSE_FILE.is_file():
        return f"compose file not found at {COMPOSE_FILE}"

    probe = _run(["docker", "compose", "version"], timeout=60.0)
    if probe.returncode != 0:
        return (
            "the docker compose plugin is unavailable "
            f"({_first_line(probe) or 'no output'}) — update Docker to v2 or install "
            "docker-compose-plugin"
        )

    # `docker info` is the daemon check. The CLI can be installed and healthy
    # while nothing is listening on the socket, which is the common case on a
    # laptop where Docker Desktop simply is not running.
    info = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=120.0)
    if info.returncode != 0:
        return (
            "the Docker daemon is not reachable "
            f"({_first_line(info) or 'no output'}) — start Docker Desktop and re-run"
        )

    return None


@dataclass
class ComposeStack:
    """
    The `publishhub` compose project, as a test sees it.

    `ensure_up` is the only entry point that changes global state, and it records
    whether the stack was already running in `was_preexisting` so teardown can
    decide between "stop what I started" and "leave the developer's stack alone".
    """

    compose_file: Path = COMPOSE_FILE
    api_base_url: str = DEFAULT_API_BASE_URL
    startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS
    was_preexisting: bool = field(default=False, init=False)
    #: The adopted worker's simulation settings, captured before this suite
    #: replaces the container, so they can be put back exactly as found. Empty
    #: when the suite started the stack itself.
    adopted_worker_env: dict[str, str] = field(default_factory=dict, init=False)

    # --- compose ------------------------------------------------------------

    def compose(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        timeout: float = 300.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """One `docker compose` invocation against this project's file."""
        command = ["docker", "compose", "--file", str(self.compose_file), *args]
        result = _run(command, env=env, timeout=timeout)
        if check and result.returncode != 0:
            raise StackError(
                f"command failed ({result.returncode}): {' '.join(command)}\n"
                f"{_tail(result.stdout)}{_tail(result.stderr)}"
            )
        return result

    def running_services(self) -> set[str]:
        """Names of the project's services with a running container."""
        result = self.compose("ps", "--status", "running", "--format", "json", timeout=120.0)
        names: set[str] = set()
        # Compose emits either a JSON array or one object per line depending on
        # version, so both shapes are accepted.
        text = result.stdout.strip()
        if not text:
            return names
        if text.startswith("["):
            for entry in json.loads(text):
                names.add(str(entry.get("Service", "")))
        else:
            for line in text.splitlines():
                line = line.strip()
                if line:
                    names.add(str(json.loads(line).get("Service", "")))
        names.discard("")
        return names

    def ensure_up(self, *, worker_env: Mapping[str, str] = BASELINE_WORKER_ENV) -> None:
        """
        Bring the stack up, or adopt one that is already running.

        `--wait` is what makes this a real readiness gate: compose blocks until
        every service passes the health check defined in docker-compose.yaml and
        exits non-zero if one does not, so a successful return means the API
        answers `/health` and the worker has written its readiness marker.

        Adoption exists because the compose project name is fixed and its ports
        are published on loopback. Starting a second copy would collide on both,
        and quietly recreating a stack a developer is using mid-session would be
        worse than reusing it.

        Only `TESTED_SERVICES` are started. The web frontend has its own unit
        suite and takes no part in these assertions, so starting it would buy an
        `npm install` and a Vite boot for nothing. An adopted stack that already
        runs it is left alone.
        """
        already = self.running_services()
        self.was_preexisting = set(TESTED_SERVICES).issubset(already)

        if self.was_preexisting:
            # Read the settings before touching anything, so the dead-letter test
            # can hand the developer back the worker it borrowed rather than one
            # configured to this suite's taste.
            self.adopted_worker_env = self.worker_env(BASELINE_WORKER_ENV.keys())
            # Still wait: running is not the same as healthy, and an adopted
            # stack may have been started seconds ago.
            self.compose(
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                str(int(self.startup_timeout_seconds)),
                "--no-recreate",
                timeout=self.startup_timeout_seconds + 60.0,
            )
            return

        self.compose(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            str(int(self.startup_timeout_seconds)),
            *TESTED_SERVICES,
            env=worker_env,
            timeout=self.startup_timeout_seconds + 60.0,
        )

    def worker_env(self, keys: Iterable[str]) -> dict[str, str]:
        """
        The worker container's current values for `keys`, as compose variables.

        Read from the container rather than from the environment, because
        docker-compose.yaml resolves each of these through `${VAR:-default}` — the
        value in effect is the one the running container was given, which is the
        only thing worth restoring. Keys the container does not define are omitted
        so that restoring them falls back to the compose default.
        """
        listed = self.compose("ps", "--quiet", "worker", timeout=60.0, check=False)
        container_ids = listed.stdout.split()
        if not container_ids:
            return {}

        inspected = _run(
            ["docker", "inspect", container_ids[0], "--format", "{{json .Config.Env}}"],
            timeout=60.0,
        )
        if inspected.returncode != 0:
            return {}

        try:
            entries = json.loads(inspected.stdout or "[]")
        except json.JSONDecodeError:
            return {}

        wanted = set(keys)
        found: dict[str, str] = {}
        for entry in entries:
            name, separator, value = str(entry).partition("=")
            if separator and name in wanted:
                found[name] = value
        return found

    def recreate_worker(self, env: Mapping[str, str]) -> None:
        """
        Replace the worker container so it re-reads its environment.

        The worker's simulation and retry settings are read once at startup, so
        exercising the dead-letter path means a new container rather than a new
        request. `--no-deps` keeps Redis and the API — and the post records and
        queue contents in Redis — exactly as they are.
        """
        self.compose(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            str(int(self.startup_timeout_seconds)),
            "--no-deps",
            "--force-recreate",
            "worker",
            env=env,
            timeout=self.startup_timeout_seconds + 60.0,
        )

    def down(self) -> None:
        """Stop the stack, keeping the dependency-cache volumes for the next run."""
        self.compose("down", "--remove-orphans", timeout=300.0, check=False)

    def logs(self, service: str, *, tail: int = 40) -> str:
        result = self.compose(
            "logs", "--no-color", "--tail", str(tail), service, timeout=120.0, check=False
        )
        return f"{result.stdout}{result.stderr}".strip()

    def diagnostics(self, *, tail: int = 25) -> str:
        """Recent api and worker logs, for a failure message that explains itself."""
        return "\n".join(
            f"--- {service} (last {tail} lines) ---\n{self.logs(service, tail=tail)}"
            for service in ("api", "worker")
        )

    # --- redis --------------------------------------------------------------

    def redis_cli(self, *args: str) -> str:
        """
        Run `redis-cli` inside the redis container and return its stdout.

        Talking to Redis through the container rather than the published port
        keeps the assertion working even if a developer has remapped 6379, and
        needs no Python Redis client in this suite's requirements.
        """
        result = self.compose(
            "exec", "--no-TTY", "redis", "redis-cli", *args, timeout=60.0
        )
        return result.stdout.strip()

    def list_length(self, key: str) -> int:
        return int(self.redis_cli("LLEN", key) or "0")

    def list_entries(self, key: str) -> list[str]:
        """
        Every element of a Redis list, newest first.

        One entry per output line, which is safe here because queue payloads are
        compact single-line JSON — the same property that lets a dead-lettered
        message be replayed verbatim.
        """
        raw = self.redis_cli("LRANGE", key, "0", "-1")
        return [line for line in raw.splitlines() if line.strip()]

    # --- api ----------------------------------------------------------------

    def publish(self, content: str, platforms: Sequence[str]) -> tuple[int, Any]:
        return post_json(
            f"{self.api_base_url}/api/v1/publish",
            {"content": content, "platforms": list(platforms)},
        )

    def post_record(self, post_id: str) -> tuple[int, Any]:
        return get_json(f"{self.api_base_url}/api/v1/posts/{post_id}")


# --- HTTP -------------------------------------------------------------------


def post_json(url: str, payload: Mapping[str, Any], *, timeout: float = 15.0) -> tuple[int, Any]:
    body = json.dumps(payload).encode("utf-8")
    # Plain http to a fixed loopback address: that is what the compose stack
    # publishes, and the API is deliberately not reachable from anywhere else.
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"content-type": "application/json", "accept": "application/json"},
    )
    return _send(request, timeout=timeout)


def get_json(url: str, *, timeout: float = 15.0) -> tuple[int, Any]:
    request = urllib.request.Request(url, method="GET", headers={"accept": "application/json"})
    return _send(request, timeout=timeout)


def _send(request: urllib.request.Request, *, timeout: float) -> tuple[int, Any]:
    """
    Status and decoded body, with a non-2xx treated as data rather than an error.

    `urllib` raises on 4xx and 5xx, but a 404 from `GET /api/v1/posts/:id` is a
    perfectly good intermediate observation while polling, so the exception is
    unwrapped back into the status and body it carries.
    """
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as error:
        return error.code, _decode(error.read())


def _decode(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


# --- waiting ----------------------------------------------------------------


def wait_for(
    probe: Callable[[], T | None],
    *,
    timeout_seconds: float,
    description: str,
    interval_seconds: float = 0.5,
    context: Callable[[], str] | None = None,
) -> T:
    """
    Poll `probe` until it returns something other than `None`.

    Polling, rather than a fixed sleep, is what keeps the suite fast when the
    stack is quick and reliable when it is not. On timeout the failure names what
    was being waited for and appends `context()` — recent service logs — because
    "post never reached a terminal status" on its own sends the reader to
    `docker compose logs` anyway.
    """
    deadline = time.monotonic() + timeout_seconds
    last: T | None = None
    while time.monotonic() < deadline:
        last = probe()
        if last is not None:
            return last
        time.sleep(interval_seconds)

    detail = f"\n{context()}" if context is not None else ""
    raise AssertionError(
        f"timed out after {timeout_seconds:.0f}s waiting for {description}{detail}"
    )


# --- subprocess -------------------------------------------------------------


def _run(
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """
    Capture-everything subprocess call. Argument list, never a shell string.

    Overrides are layered onto the ambient environment rather than replacing it,
    because compose needs `PATH`, `HOME`, and `DOCKER_HOST` to work at all.
    """
    merged = {**os.environ, **(env or {})}
    try:
        # Argument list, never `shell=True`: a service name or a Redis key cannot
        # become a shell command.
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=merged,
            cwd=REPO_ROOT,
            check=False,
        )
    except subprocess.TimeoutExpired as expired:
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=124,
            stdout=expired.stdout or "" if isinstance(expired.stdout, str) else "",
            stderr=f"timed out after {timeout:.0f}s",
        )


def _first_line(result: subprocess.CompletedProcess[str]) -> str:
    for stream in (result.stderr, result.stdout):
        for line in (stream or "").splitlines():
            if line.strip():
                return line.strip()
    return ""


def _tail(text: str, *, limit: int = 2000) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return f"{text}\n"
    return f"…{text[-limit:]}\n"
