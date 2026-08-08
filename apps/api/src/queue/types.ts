/**
 * Queue abstraction — shared types.
 *
 * The API and the worker both talk to `QueueClient`; nothing above this layer
 * knows whether the active backend is a Redis list or an Amazon SQS queue
 * (Requirements 5.1, 5.4). The wire format is defined once in
 * `docs/message-schema.md` and mirrored by `PublishJob` here and by the Python
 * dataclass in `apps/worker` (Requirement 5.6).
 */

/** The only envelope version this build produces or accepts. */
export const SCHEMA_VERSION = 1;

/** Publish targets, lowercase and exact-match (docs/message-schema.md). */
export type Platform = 'twitter' | 'linkedin' | 'mastodon' | 'bluesky';

/**
 * The message envelope. Every field is required; a producer with nothing to say
 * for `trace_context` sends `{}` rather than omitting the key or sending null.
 */
export interface PublishJob {
  schema_version: typeof SCHEMA_VERSION;
  /** UUID v4, lowercase. Stable across retries so all attempts correlate. */
  job_id: string;
  /** `post_` + 26-char Crockford base32 ULID. Key of the Redis post record. */
  post_id: string;
  /** 1–5000 characters, not blank after trimming. Never truncated here. */
  content: string;
  /** Non-empty, no duplicates, submission order preserved. */
  platforms: Platform[];
  /** Delivery attempt number, one-based. */
  attempt: number;
  /** RFC 3339 UTC with millisecond precision, e.g. `2026-08-07T10:00:00.000Z`. */
  enqueued_at: string;
  /** Datadog propagation headers, `{}` when tracing is off. Treated as opaque. */
  trace_context: Record<string, string>;
}

/** Terminal dead-letter reasons from docs/message-schema.md. */
export type DeadLetterReason =
  | 'unparseable_payload'
  | 'unknown_schema_version'
  | 'schema_validation_failed'
  | 'max_attempts_exhausted';

/**
 * Backend-specific claim handle. The envelope itself never carries a receipt
 * handle or processing-list membership: those belong to the queue client.
 */
export type JobHandle =
  | { readonly backend: 'redis'; readonly payload: string }
  | { readonly backend: 'sqs'; readonly messageId: string; readonly receiptHandle: string };

/**
 * A claimed message. `job` is null when the payload failed validation, because
 * a consumer still has to dead-letter what it cannot parse — receive never
 * throws on a bad payload, it reports it (Requirement 3.4).
 */
export interface ReceivedJob {
  /** Exactly the text that was on the queue, so a dead letter stays replayable. */
  readonly raw: string;
  readonly handle: JobHandle;
  readonly job: PublishJob | null;
  readonly invalidReason: DeadLetterReason | null;
  /** Human-readable explanation of the rejection, for logs. */
  readonly invalidDetail: string | null;
}

/** Emitted whenever a message is dead-lettered, for structured logging. */
export interface DeadLetterEvent {
  readonly backend: QueueBackend;
  readonly reason: string;
  readonly jobId: string | null;
  readonly postId: string | null;
  readonly attempt: number | null;
  /**
   * True when the message was left in place for the SQS redrive policy to move
   * rather than being sent to an explicitly configured dead-letter queue.
   */
  readonly viaRedrivePolicy: boolean;
}

export interface QueueClient {
  enqueue(job: PublishJob): Promise<void>;
  /**
   * Claim one message, blocking (Redis) or long-polling (SQS) for at most
   * `waitSeconds`. Returns null when nothing arrived in that window.
   * `waitSeconds <= 0` means "do not wait" in both backends.
   */
  receive(waitSeconds: number): Promise<ReceivedJob | null>;
  ack(job: ReceivedJob): Promise<void>;
  deadLetter(job: ReceivedJob, reason: string): Promise<void>;
  /** Pending message count, the same number KEDA scales on. */
  depth(): Promise<number>;
  close(): Promise<void>;
}

export type QueueBackend = 'redis' | 'sqs';

/**
 * Thrown at startup when the selected backend is unknown or a required setting
 * for it is missing. `key` names the offending environment variable so the
 * failure is actionable rather than opaque (Requirement 5.5).
 */
export class QueueConfigError extends Error {
  readonly key: string;

  constructor(key: string, message: string) {
    super(message);
    this.name = 'QueueConfigError';
    this.key = key;
  }
}
