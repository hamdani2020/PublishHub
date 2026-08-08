# Message Schema — `PublishJob` envelope

The single contract between the API (TypeScript) and the worker (Python), and
the single format written to both queue backends (Redis list, Amazon SQS).
Nothing above the queue abstraction branches on backend or language, so this
document is the only place the wire format is defined (Requirement 5.6).

- Canonical fixture: [`contracts/publish-job.v1.fixture.json`](../contracts/publish-job.v1.fixture.json)
- Mirrored by: the TypeScript `PublishJob` type (`apps/api`) and the Python
  `PublishJob` dataclass (`apps/worker`)
- Both test suites assert against the fixture, so the two implementations
  cannot drift apart silently

## Current version

`schema_version: 1`

## Example

```json
{
  "schema_version": 1,
  "job_id": "3f2a9b0c-5d41-4e8b-9c2a-7d6e5f4a3b21",
  "post_id": "post_01HZX3QK7M9V4TDR8N2C5EAB6F",
  "content": "Shipping PublishHub: kind + ArgoCD + KEDA, all from one Makefile.",
  "platforms": ["twitter", "linkedin"],
  "attempt": 1,
  "enqueued_at": "2026-08-07T10:00:00.000Z",
  "trace_context": {
    "x-datadog-trace-id": "6249442685991245312",
    "x-datadog-parent-id": "8114249130118331704",
    "x-datadog-sampling-priority": "1"
  }
}
```

## Fields

Every field is required. There are no optional fields in version 1; a producer
that has nothing to say for `trace_context` sends `{}` rather than omitting the
key or sending `null`.

| Field | Type | Constraints | Meaning |
|---|---|---|---|
| `schema_version` | integer | exactly `1` | Envelope version. Consumers dead-letter any other value instead of guessing the shape. |
| `job_id` | string | UUID v4, lowercase | Identifies this unit of work. Generated once by the API and **kept stable across retries**, so all attempts of one job correlate in logs. |
| `post_id` | string | `post_` + 26-character Crockford base32 ULID, matching `^post_[0-9A-HJKMNP-TV-Z]{26}$` | Key of the post record in Redis and the `id` returned to the client by `POST /api/v1/publish`. |
| `content` | string | 1–5000 characters, UTF-8, not blank after trimming | The post body exactly as submitted. Never truncated or normalized in the envelope. |
| `platforms` | array of string | non-empty, no duplicates, each member in the allow-list below | Publish targets, in the order the client submitted them. |
| `attempt` | integer | `>= 1`, starts at `1` | Delivery attempt number. The worker increments it when re-enqueueing after a failure; when it would exceed `MAX_ATTEMPTS` the job is dead-lettered instead. |
| `enqueued_at` | string | RFC 3339 / ISO 8601, UTC, millisecond precision, trailing `Z`, matching `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$` | When **this** enqueue happened. Refreshed on re-enqueue, so it measures queue wait for the current attempt. The original submission time lives on the post record in Redis. |
| `trace_context` | object | string keys, string values; `{}` when tracing is off | Datadog distributed-trace propagation headers, so the worker span links to the originating API request. |

### Platform allow-list

`twitter`, `linkedin`, `mastodon`, `bluesky` — lowercase, exact match. The API
rejects anything else with `400 VALIDATION_FAILED` and does not enqueue
(Requirement 2.2). Adding a platform means adding it here, to the API's
validation allow-list, and to the worker's simulator.

### `trace_context` keys

Populated only when `OBSERVABILITY_ENABLED=true`. Recognized keys:

| Key | Notes |
|---|---|
| `x-datadog-trace-id` | Present whenever tracing is active |
| `x-datadog-parent-id` | Span id of the API handler; becomes the worker span's parent |
| `x-datadog-sampling-priority` | Optional, propagated verbatim when present |
| `x-datadog-tags` | Optional, propagated verbatim when present |

Consumers treat this map as opaque: unknown keys are passed to the tracer, not
validated. An empty object means "no parent" and the worker starts a root span
rather than failing (Requirement 14.2, Requirement 14.6).

## Serialization and transport

- UTF-8 encoded JSON object. No `NaN`, `Infinity`, byte-order mark, or trailing
  content after the object.
- Key order is not significant. Consumers must not depend on it.
- **Redis:** the JSON text is the list element. `LPUSH publishhub:jobs`,
  received with `BRPOPLPUSH publishhub:jobs publishhub:jobs:processing`,
  dead-lettered to `publishhub:jobs:dlq`.
- **SQS:** the JSON text is the `MessageBody`. No contract data is carried in
  `MessageAttributes`, so the two backends stay byte-comparable and a message
  captured from one can be replayed into the other.
- The envelope carries no queue-specific handle. Receipt handles, Redis
  processing-list membership, and visibility windows belong to the queue client,
  not to the message.

## Validation and failure behavior

Consumers validate before doing any work, and every rejection is terminal
rather than retried, because a malformed message will not become well-formed on
a second read (Requirement 3.4).

| Condition | Behavior |
|---|---|
| Body is not valid JSON, or not a JSON object | Dead-letter immediately with reason `unparseable_payload`; log the raw payload truncated |
| `schema_version` missing or not `1` | Dead-letter immediately with reason `unknown_schema_version` |
| A required field is missing, null, or of the wrong type | Dead-letter immediately with reason `schema_validation_failed` |
| `content` empty or over 5000 characters | Rejected by the API at request time; if seen by the worker, dead-letter with reason `schema_validation_failed` |
| `platforms` empty or containing a value outside the allow-list | Same as above |
| Processing raises after successful validation | Retry with exponential backoff, `attempt` incremented, up to `MAX_ATTEMPTS`, then dead-letter with reason `max_attempts_exhausted` |

## Versioning policy

`schema_version` is a single integer, not semver, because there is exactly one
producer and one consumer and they must agree exactly.

- **No bump** for additive, ignorable changes: a new field that consumers can
  safely skip. Consumers therefore ignore unknown top-level fields rather than
  rejecting them, which keeps a rolling deploy safe while old and new pods run
  side by side.
- **Bump** for anything else: removing or renaming a field, changing a type,
  tightening a constraint, or changing the meaning of an existing field.
- When the version is bumped, the consumer supports both versions for at least
  one release before dropping the old one; until it does, old messages
  dead-letter with an explicit reason instead of being misread.
- The fixture gains a section per supported version. Removing a version from the
  fixture is the signal that support was dropped.

## Using the fixture in tests

`contracts/publish-job.v1.fixture.json` is resolved relative to the repository
root so both suites read the same bytes.

```ts
// apps/api — Vitest
import fixture from '../../../contracts/publish-job.v1.fixture.json';

const job = fixture.canonical;
```

```python
# apps/worker — pytest
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = json.loads(
    (REPO_ROOT / "contracts" / "publish-job.v1.fixture.json").read_text()
)
job = FIXTURE["canonical"]
```

The fixture's top-level keys:

| Key | Purpose |
|---|---|
| `schema_version` | The version this fixture describes |
| `required_fields` | Exact field set. Assert your type/dataclass round-trips these and nothing else, which is what catches drift. |
| `constraints` | Content length bounds, platform allow-list, and the regexes above, so both languages test the same limits |
| `canonical` | The reference message. Serialize your type and compare against it. |
| `variants` | Array of `{ name, description, message }` for valid messages that exercise the edges: tracing disabled, a retry attempt, all platforms, non-ASCII content, and an unknown field that must be ignored rather than rejected |
| `invalid` | Array of `{ name, reason, message }` — or `{ name, reason, raw }` when the payload is not valid JSON at all — each carrying the expected dead-letter reason from the table above |

Content-length bounds are asserted from `constraints.content_max_length` rather
than from a 5000-character literal in the fixture, so both suites build the
boundary strings themselves and the fixture stays readable.
