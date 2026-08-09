"""
PublishHub background worker.

| Package      | Responsibility                                              | Spec task |
|--------------|-------------------------------------------------------------|-----------|
| `queue`         | Redis / SQS abstraction and the message envelope codec    | 2.3       |
| `config`        | Startup configuration, validated once, failing on a bad key| 4.1      |
| `logging`       | Structured JSON logs with service, env, and trace fields  | 4.1       |
| `posts`         | The worker's write side of the post record in Redis       | 4.2       |
| `processing`    | The job loop: claim, publish, record, ack, retry, dead-letter| 4.2, 4.3|
| `resilience`    | Exponential backoff, the startup queue wait, readiness    | 4.3       |
| `runtime`       | The `SIGTERM` stop flag and the process entrypoint        | 4.4       |
| `observability` | Custom metrics and Datadog tracing, behind one switch     | 4.5       |

The process starts as `python -m publishhub_worker`: `__main__.py` hands the exit
code from `runtime.main` to `SystemExit`, so a bad configuration key exits 1 with
one readable line and a graceful stop exits 0.

`observability` is last in that table and first in the entrypoint's imports, because
a tracer can only patch a module that has not been evaluated yet. With
`OBSERVABILITY_ENABLED` off — the default — it loads no APM library, opens no socket,
and adds no call to the broker (Requirement 14.6).
"""

__all__ = [
    "config",
    "logging",
    "observability",
    "posts",
    "processing",
    "queue",
    "resilience",
    "runtime",
]
