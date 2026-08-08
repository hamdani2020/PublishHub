/**
 * Envelope codec: serialize, parse, and validate the `PublishJob` message.
 *
 * The rules here are the ones written down in `docs/message-schema.md` and
 * asserted by `contracts/publish-job.v1.fixture.json`. Both languages read that
 * fixture in their test suites, so the two implementations cannot drift
 * (Requirement 5.6).
 *
 * Validation never throws on bad input from the queue: it returns a reason, so
 * the caller can dead-letter the message instead of crash-looping on it
 * (Requirement 3.4).
 */

import { randomUUID } from 'node:crypto';

import { SCHEMA_VERSION } from './types.js';
import type { DeadLetterReason, Platform, PublishJob } from './types.js';

export const CONTENT_MIN_LENGTH = 1;
export const CONTENT_MAX_LENGTH = 5000;

export const PLATFORM_ALLOW_LIST: readonly Platform[] = [
  'twitter',
  'linkedin',
  'mastodon',
  'bluesky',
];

export const JOB_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
export const POST_ID_PATTERN = /^post_[0-9A-HJKMNP-TV-Z]{26}$/;
export const ENQUEUED_AT_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;

/** Field order used when serializing. Key order is not part of the contract,
 * but a stable order keeps fixtures and log lines diffable. */
export const FIELD_ORDER = [
  'schema_version',
  'job_id',
  'post_id',
  'content',
  'platforms',
  'attempt',
  'enqueued_at',
  'trace_context',
] as const;

export type ParseResult =
  | { readonly ok: true; readonly job: PublishJob }
  | { readonly ok: false; readonly reason: DeadLetterReason; readonly detail: string };

/**
 * Length in Unicode code points, not UTF-16 code units, so the 5000-character
 * bound means the same thing here as it does to Python's `len()`.
 */
export function characterLength(value: string): number {
  return Array.from(value).length;
}

export function isPlatform(value: unknown): value is Platform {
  return typeof value === 'string' && (PLATFORM_ALLOW_LIST as readonly string[]).includes(value);
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function invalid(reason: DeadLetterReason, detail: string): ParseResult {
  return { ok: false, reason, detail };
}

/**
 * Validate an already-decoded value against version 1 of the envelope.
 *
 * Unknown top-level fields are ignored rather than rejected, so a rolling
 * deploy where a newer producer adds a field stays safe. The returned job
 * contains only the known fields.
 */
export function validatePublishJob(value: unknown): ParseResult {
  if (!isPlainObject(value)) {
    return invalid('unparseable_payload', 'payload is not a JSON object');
  }

  // Checked first: without a version we do not know which shape to expect, and
  // guessing is worse than dead-lettering.
  if (value['schema_version'] !== SCHEMA_VERSION) {
    return invalid(
      'unknown_schema_version',
      `schema_version must be ${SCHEMA_VERSION}, received ${JSON.stringify(value['schema_version'])}`,
    );
  }

  const jobId = value['job_id'];
  if (typeof jobId !== 'string' || !JOB_ID_PATTERN.test(jobId)) {
    return invalid('schema_validation_failed', 'job_id must be a lowercase UUID v4 string');
  }

  const postId = value['post_id'];
  if (typeof postId !== 'string' || !POST_ID_PATTERN.test(postId)) {
    return invalid(
      'schema_validation_failed',
      'post_id must match ^post_[0-9A-HJKMNP-TV-Z]{26}$',
    );
  }

  const content = value['content'];
  if (typeof content !== 'string') {
    return invalid('schema_validation_failed', 'content must be a string');
  }
  if (content.trim().length < CONTENT_MIN_LENGTH) {
    return invalid('schema_validation_failed', 'content must not be blank');
  }
  if (characterLength(content) > CONTENT_MAX_LENGTH) {
    return invalid(
      'schema_validation_failed',
      `content must be at most ${CONTENT_MAX_LENGTH} characters`,
    );
  }

  const platforms = value['platforms'];
  if (!Array.isArray(platforms) || platforms.length === 0) {
    return invalid('schema_validation_failed', 'platforms must be a non-empty array');
  }
  const unsupported = platforms.filter((platform) => !isPlatform(platform));
  if (unsupported.length > 0) {
    return invalid(
      'schema_validation_failed',
      `platforms contains unsupported target(s): ${unsupported.map((p) => JSON.stringify(p)).join(', ')}`,
    );
  }
  if (new Set(platforms).size !== platforms.length) {
    return invalid('schema_validation_failed', 'platforms must not contain duplicates');
  }

  const attempt = value['attempt'];
  if (typeof attempt !== 'number' || !Number.isInteger(attempt) || attempt < 1) {
    return invalid('schema_validation_failed', 'attempt must be an integer >= 1');
  }

  const enqueuedAt = value['enqueued_at'];
  if (typeof enqueuedAt !== 'string' || !ENQUEUED_AT_PATTERN.test(enqueuedAt)) {
    return invalid(
      'schema_validation_failed',
      'enqueued_at must be UTC RFC 3339 with millisecond precision, e.g. 2026-08-07T10:00:00.000Z',
    );
  }

  const traceContext = value['trace_context'];
  if (!isPlainObject(traceContext)) {
    return invalid(
      'schema_validation_failed',
      'trace_context must be an object; send {} when tracing is off, never null',
    );
  }
  for (const [key, headerValue] of Object.entries(traceContext)) {
    if (typeof headerValue !== 'string') {
      return invalid(
        'schema_validation_failed',
        `trace_context.${key} must be a string`,
      );
    }
  }

  return {
    ok: true,
    job: {
      schema_version: SCHEMA_VERSION,
      job_id: jobId,
      post_id: postId,
      content,
      platforms: platforms as Platform[],
      attempt,
      enqueued_at: enqueuedAt,
      trace_context: traceContext as Record<string, string>,
    },
  };
}

/** Decode queue bytes into a validated job, or report why it cannot be used. */
export function parsePublishJob(raw: string): ParseResult {
  let decoded: unknown;
  try {
    decoded = JSON.parse(raw) as unknown;
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return invalid('unparseable_payload', `payload is not valid JSON: ${detail}`);
  }
  return validatePublishJob(decoded);
}

/** Encode a job as the UTF-8 JSON text written to either backend. */
export function serializePublishJob(job: PublishJob): string {
  return JSON.stringify({
    schema_version: job.schema_version,
    job_id: job.job_id,
    post_id: job.post_id,
    content: job.content,
    platforms: job.platforms,
    attempt: job.attempt,
    enqueued_at: job.enqueued_at,
    trace_context: job.trace_context,
  });
}

/** RFC 3339 UTC with millisecond precision — the `enqueued_at` format. */
export function formatEnqueuedAt(date: Date = new Date()): string {
  return date.toISOString().replace(/\.(\d{3})\d*Z$/, '.$1Z');
}

export interface CreatePublishJobInput {
  post_id: string;
  content: string;
  platforms: Platform[];
  /** Defaults to 1. The worker passes an incremented value when re-enqueueing. */
  attempt?: number | undefined;
  /** Defaults to a fresh UUID v4. Kept stable across retries by the caller. */
  job_id?: string | undefined;
  /** Defaults to now. Refreshed on every enqueue, including retries. */
  enqueued_at?: string | undefined;
  /** Defaults to `{}`, meaning tracing is off and the worker starts a root span. */
  trace_context?: Record<string, string> | undefined;
}

/**
 * Build a valid envelope, throwing if the result would not be valid. Producing
 * a malformed message is a programming error on this side of the queue, unlike
 * receiving one, which is a runtime condition handled by dead-lettering.
 */
export function createPublishJob(input: CreatePublishJobInput): PublishJob {
  const candidate: PublishJob = {
    schema_version: SCHEMA_VERSION,
    job_id: input.job_id ?? randomUUID(),
    post_id: input.post_id,
    content: input.content,
    platforms: input.platforms,
    attempt: input.attempt ?? 1,
    enqueued_at: input.enqueued_at ?? formatEnqueuedAt(),
    trace_context: input.trace_context ?? {},
  };

  const result = validatePublishJob(candidate);
  if (!result.ok) {
    throw new TypeError(`cannot create PublishJob: ${result.detail}`);
  }
  return result.job;
}

/** Fields useful for structured logs, tolerant of a payload that never parsed. */
export function describeJob(job: PublishJob | null): {
  jobId: string | null;
  postId: string | null;
  attempt: number | null;
} {
  return {
    jobId: job?.job_id ?? null,
    postId: job?.post_id ?? null,
    attempt: job?.attempt ?? null,
  };
}
