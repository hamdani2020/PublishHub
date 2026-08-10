# PublishHub worker — production image (Requirements 6.2, 6.3, 6.6, 3.6).
#
# Two stages. The first resolves and installs the runtime dependencies into a
# self-contained virtualenv at /opt/venv; the second is a fresh base image that
# receives that directory and the package source, and nothing else. What stays
# behind in stage one is everything Requirement 6.2 is about: pip's wheel cache,
# any compiler or apt layer an install needed, and — because the install reads
# requirements.txt and never requirements-dev.txt — pytest and ruff.
#
# The virtualenv is the whole trick. It makes "the dependencies" a single
# directory that can be copied between stages, which is what lets stage two be a
# clean base image rather than stage one with things deleted from it. Deleting is
# how you get an image whose layers still contain what you deleted.
#
# Three properties are load-bearing at deploy time and easy to break by accident:
#
# 1. **Pinned base image.** `python:3.11.15-slim-bookworm`, not `python:3.11-slim`
#    (Requirement 6.6). Same tag docker-compose.yaml uses, so the interpreter that
#    runs the test suite is the interpreter that runs in the cluster. Dependency
#    versions are pinned exactly in requirements.txt — `redis==8.1.0`,
#    `boto3==1.43.67`, `ddtrace==4.8.2`, no ranges — so this file installs a
#    specific tree without restating a single version.
# 2. **Non-root.** A system user `worker` (uid 10001) is created explicitly,
#    because the official Python images, unlike the Node ones, ship no non-root
#    user. Application files are owned by root, so the runtime user reads the app
#    and writes nowhere in it (Requirement 6.3). The one path the process does
#    write is `/tmp/publishhub-worker.ready`, the readiness marker
#    resilience/readiness.py maintains and the chart's probe reads, so this image
#    runs unchanged under `readOnlyRootFilesystem: true` given a `/tmp` emptyDir
#    — and it must be an emptyDir, or the probe can never pass.
# 3. **Signals reach the process.** `CMD` is exec form with `python` as argv[0],
#    so `python` is PID 1 and Kubernetes' `SIGTERM` is delivered straight to it.
#    No `sh -c` wrapper: a shell in front of the process is the classic way to
#    lose a signal, and here that would mean KEDA scaling to zero killing a worker
#    mid-job instead of letting it finish and ack (Requirements 3.6, 9.6). The
#    handler that receives it is `install_shutdown_handlers` in
#    runtime/shutdown.py, reached through `python -m publishhub_worker` — the
#    module form, so there is no wrapper script between the signal and the
#    handler either.
#
# No init shim (tini, dumb-init) on purpose: this process spawns no children and
# receives the signal directly, so an init would add a package to scan and patch
# in exchange for nothing. Note also that PID 1 is a special case in Python's
# favour here — the default disposition of `SIGTERM` is ignored for PID 1, so a
# process that did *not* install a handler would sit there until `SIGKILL`. The
# handler is what makes this safe, and it is unit-tested in tests/test_runtime.py.
#
# Build from this directory:
#   docker build -t publishhub-worker:dev apps/worker
# `make apps-build` (spec task 7.3) does this for all three services and tags each
# with both `latest` and the Git commit SHA.

# Declared once, used by both stages, so an upgrade is a one-line change and the
# venv can never be built by a different interpreter than the one that runs it —
# which would break it, since a venv records the absolute path of its base Python.
ARG PYTHON_IMAGE=python:3.11.15-slim-bookworm

# ---------------------------------------------------------------------------
# Stage 1: dependencies.
#
# Only requirements.txt is copied, so this layer stays cached until a dependency
# actually changes; editing a source file does not re-download the tree.
#
# pip itself is deliberately *not* upgraded. The base image tag is pinned, and its
# pip is part of what that pin fixes; `pip install --upgrade pip` would quietly
# reintroduce a floating version into an otherwise reproducible build
# (Requirement 6.6). Upgrading it is a deliberate edit to the pinned base tag.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS deps

# `--no-cache-dir` is set as an environment variable rather than a flag so it
# covers every pip invocation in this stage. A wheel cache in a layer is bytes
# nobody reads back.
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# A virtualenv rather than a `--user` or system install, so the artifact stage two
# needs is exactly one directory.
RUN python -m venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt ./

RUN pip install --requirement requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: runtime.
#
# A fresh base image, not a continuation of stage one, so nothing from the install
# can be inherited implicitly — this layer contains the venv and the source, and
# that is all.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# `PATH` ahead of everything else is what makes bare `python` and `pip` resolve to
# the virtualenv; activating a venv is nothing more than this. Doing it through
# `PATH` rather than a shell activation script is also what keeps the `CMD` free
# of a shell (see the signal note above).
ENV PATH="/opt/venv/bin:${PATH}" \
    # Unbuffered, so a structured log line reaches `kubectl logs` when it is
    # emitted rather than when a 4KB buffer fills. Non-negotiable for a service
    # whose logs are the primary signal (Requirement 3.5).
    PYTHONUNBUFFERED=1 \
    # No `.pyc` files: the source is read-only at run time and writing bytecode
    # would be the process's only reason to want otherwise.
    PYTHONDONTWRITEBYTECODE=1 \
    # A traceback from a failed import is more useful than a hash-collision
    # defence in a container that serves no untrusted input.
    PYTHONHASHSEED=random \
    # Matches pytest.ini's `pythonpath = src` and docker-compose.yaml, so
    # `publishhub_worker` is importable from the same layout in all three places,
    # with no editable install and no packaging step.
    PYTHONPATH=/app/src

# uid 10001, no home directory, no login shell: this account exists to own a
# process, not to be used. Referenced by name here and asserted numerically by the
# chart's `runAsUser`/`runAsNonRoot` (task 9.2), which is what makes the
# Kubernetes-level guarantee independent of this line.
RUN groupadd --system --gid 10001 worker \
 && useradd --system --uid 10001 --gid 10001 \
      --home-dir /app --no-create-home --shell /usr/sbin/nologin worker

WORKDIR /app

# Owned by root, run as `worker`: readable and executable by the runtime user,
# writable by nobody (Requirement 6.3). `--chown` is stated rather than left
# implicit so that intent survives a future edit.
COPY --from=deps --chown=root:root /opt/venv /opt/venv
COPY --chown=root:root src ./src

USER worker

# No `EXPOSE`: the worker listens on nothing. It is a queue consumer, its
# readiness is a file, and it needs no Service — which is also why KEDA can scale
# it to zero without anything losing an endpoint.

# Exec form, module form. See the signal note in the header — this is the line
# that decides whether `SIGTERM` reaches the shutdown flag or is dropped by a
# shell that never forwards it.
CMD ["python", "-m", "publishhub_worker"]
