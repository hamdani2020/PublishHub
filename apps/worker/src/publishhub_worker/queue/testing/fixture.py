"""
Loader for the shared contract fixture.

`contracts/publish-job.v1.fixture.json` is read from the repository root, the same
bytes the TypeScript suite reads, which is what stops the two implementations from
drifting (docs/message-schema.md).

Mirrors `apps/api/src/queue/testing/fixture.ts`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# fixture.py -> testing -> queue -> publishhub_worker -> src -> worker -> apps -> repository root
REPO_ROOT = Path(__file__).resolve().parents[6]

FIXTURE_PATH = REPO_ROOT / "contracts" / "publish-job.v1.fixture.json"


def load_publish_job_fixture() -> dict[str, Any]:
    if not FIXTURE_PATH.is_file():
        raise FileNotFoundError(
            f"shared contract fixture not found at {FIXTURE_PATH}; "
            "both test suites read it from the repository root"
        )
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def serialize_fixture_message(message: dict[str, Any]) -> str:
    """
    Fixture message as the exact queue payload text, matching what
    `JSON.stringify` produces on the API side: compact separators and UTF-8
    rather than `\\uXXXX` escapes.
    """
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))
