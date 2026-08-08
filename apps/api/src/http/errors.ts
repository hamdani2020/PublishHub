/**
 * The error envelope every non-2xx response uses (design.md section 3).
 *
 * ```json
 * { "error": { "code": "VALIDATION_FAILED", "message": "content must not be empty", "request_id": "..." } }
 * ```
 *
 * One shape for every failure means a client writes one error path, and
 * `request_id` is the same correlation id the request logger stamped on the log
 * line and echoed in the `x-request-id` header — so a reported failure leads
 * straight to its cause.
 *
 * The codes are the machine-readable half of Requirement 2.2. `message` is for a
 * human and is written to name the offending field and the rule it broke; it
 * never carries an internal error string, a stack, or a connection URL
 * (Requirement 2.7). The full error goes to the log instead.
 *
 * This module is the shared vocabulary: the codes and the envelope. The handler
 * that catches unhandled errors and renders the generic 500 in this shape lives
 * in `error-handler.ts`.
 */

import type { Request, Response } from 'express';

import { resolveCorrelationId } from '../logging/index.js';

/** Request body failed validation. Requirement 2.2. */
export const VALIDATION_FAILED = 'VALIDATION_FAILED';

/**
 * The queue could not be reached while enqueueing, so the submission was not
 * accepted. Design's error-handling table: `503 QUEUE_UNAVAILABLE`, logged with
 * the correlation id, and no partial post record left behind.
 */
export const QUEUE_UNAVAILABLE = 'QUEUE_UNAVAILABLE';

/**
 * A backing dependency did not answer. Used by readiness and by the publish
 * path when the post store is unreachable.
 */
export const DEPENDENCY_UNAVAILABLE = 'DEPENDENCY_UNAVAILABLE';

/**
 * The addressed resource does not exist. Used by `GET /api/v1/posts/:id`. The
 * message never echoes the requested id back, so a URL cannot be used to reflect
 * arbitrary text through the API.
 */
export const NOT_FOUND = 'NOT_FOUND';

/**
 * The request body exceeded the configured size limit. Raised by the body parser
 * before any handler runs, and mapped by the central error handler.
 */
export const PAYLOAD_TOO_LARGE = 'PAYLOAD_TOO_LARGE';

/**
 * Something failed that the service did not anticipate. The client gets this code
 * and a fixed message; the error itself only ever reaches the log
 * (Requirement 2.7).
 */
export const INTERNAL_ERROR = 'INTERNAL_ERROR';

export interface ErrorEnvelope {
  readonly error: {
    readonly code: string;
    readonly message: string;
    readonly request_id: string;
  };
}

/**
 * The correlation id assigned by the request logger, which is also the value of
 * the `x-request-id` response header. Falls back to deriving it from the
 * headers, so the envelope still carries an id if the logger is not mounted.
 */
export function resolveRequestId(req: Request): string {
  const { id } = req;
  return typeof id === 'string' || typeof id === 'number' ? String(id) : resolveCorrelationId(req);
}

export function errorEnvelope(req: Request, code: string, message: string): ErrorEnvelope {
  return { error: { code, message, request_id: resolveRequestId(req) } };
}

/** Send the envelope. `no-store`, because an error is never a cacheable answer. */
export function sendError(
  req: Request,
  res: Response,
  status: number,
  code: string,
  message: string,
): void {
  res.set('cache-control', 'no-store');
  res.status(status).json(errorEnvelope(req, code, message));
}
