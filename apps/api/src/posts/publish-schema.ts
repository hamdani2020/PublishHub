/**
 * Request validation for `POST /api/v1/publish` (Requirements 2.1, 2.2).
 *
 * The rules come from the design: `content` is a non-empty string of at most
 * 5000 characters; `platforms` is a non-empty array whose members are in the
 * allow-list; unknown body fields are stripped. The limits and the allow-list
 * itself are imported from the queue module rather than restated, because they are
 * the same numbers `docs/message-schema.md` pins for the envelope — a validator
 * that accepted something the envelope rejects would turn a 400 into a 500.
 *
 * Two deliberate choices:
 *
 * - **Unknown fields are stripped, not rejected.** `z.object` strips by default.
 *   A client that sends `status: "published"` or a stale field gets a 202 and a
 *   record built only from what it is allowed to set, rather than a confusing 400.
 * - **Duplicate platforms are deduplicated, not rejected.** Requirement 2.2
 *   enumerates the 400 cases and duplicates are not among them, but the envelope
 *   forbids them, so the request is normalized rather than failed. Submission
 *   order is preserved, first occurrence wins.
 *
 * `content` is stored and enqueued exactly as submitted — trimming only decides
 * whether it counts as empty, it never rewrites the body.
 */

import { z } from 'zod';

import {
  CONTENT_MAX_LENGTH,
  CONTENT_MIN_LENGTH,
  PLATFORM_ALLOW_LIST,
  characterLength,
  isPlatform,
} from '../queue/index.js';
import type { Platform } from '../queue/index.js';

/** First occurrence wins, so the client's ordering survives. */
function dedupePlatforms(values: readonly string[]): Platform[] {
  const seen = new Set<string>();
  const unique: Platform[] = [];
  for (const value of values) {
    if (isPlatform(value) && !seen.has(value)) {
      seen.add(value);
      unique.push(value);
    }
  }
  return unique;
}

const content = z
  .string({
    error: (issue) =>
      issue.input === undefined ? 'content is required' : 'content must be a string',
  })
  // Blank-after-trimming counts as empty: a body of spaces is not a post.
  .refine((value) => value.trim().length >= CONTENT_MIN_LENGTH, {
    error: 'content must not be empty',
  })
  // Counted in Unicode code points, so the bound means the same thing here as it
  // does to the Python worker's `len()` — an emoji is one character, not two.
  .refine((value) => characterLength(value) <= CONTENT_MAX_LENGTH, {
    error: `content must be at most ${String(CONTENT_MAX_LENGTH)} characters`,
  });

const platforms = z
  .array(z.string(), {
    error: (issue) =>
      issue.input === undefined ? 'platforms is required' : 'platforms must be an array',
  })
  .refine((values) => values.length > 0, {
    error: 'platforms must contain at least one target',
  })
  .refine((values) => values.every(isPlatform), {
    // Names the allow-list instead of echoing the rejected value: it tells the
    // client what to send, and keeps arbitrary request text out of the response.
    error: `platforms must contain only supported targets: ${PLATFORM_ALLOW_LIST.join(', ')}`,
  })
  .transform(dedupePlatforms);

export const publishRequestSchema = z.object({ content, platforms });

/** The validated, normalized request. Exactly what the handler is allowed to use. */
export interface PublishRequest {
  readonly content: string;
  readonly platforms: Platform[];
}

export type PublishRequestValidation =
  | { readonly ok: true; readonly value: PublishRequest }
  | { readonly ok: false; readonly message: string };

/**
 * One issue is reported, not all of them: the envelope carries a single message,
 * and a client fixing the first problem re-submits and learns about the next.
 * The custom messages already start with the field name; the fallback prefixes
 * the path for the issues zod words itself, such as a non-string array member.
 */
function describeFailure(error: z.ZodError): string {
  const [issue] = error.issues;
  if (issue === undefined) {
    return 'request body is invalid';
  }
  const path = issue.path.map((segment) => String(segment)).join('.');
  if (path === '') {
    return `request body ${issue.message}`;
  }
  return issue.message.startsWith(path) ? issue.message : `${path}: ${issue.message}`;
}

export function validatePublishRequest(body: unknown): PublishRequestValidation {
  const parsed = publishRequestSchema.safeParse(body);
  if (parsed.success) {
    return { ok: true, value: parsed.data };
  }
  return { ok: false, message: describeFailure(parsed.error) };
}
