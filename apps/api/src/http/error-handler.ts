/**
 * The last two middlewares in the stack: unmatched routes, and everything that
 * threw (Requirement 2.7).
 *
 * The contract is short and absolute: **a failure the service did not anticipate
 * tells the client nothing except that it failed.** No message from the thrown
 * error, no stack, no dependency name, no connection string, no internal id.
 * Those all go to the log instead, correlated by the same `request_id` the client
 * receives, which is what makes a support conversation possible without leaking
 * anything: the caller quotes the id, an operator finds the full error.
 *
 * The one class of exception is failures that are unambiguously the client's, and
 * that the client can act on. `body-parser` throws two of them — malformed JSON
 * and a body over the size limit — and both are mapped to their own status with a
 * message that names the rule and nothing else. Anything else, including an error
 * that arrives carrying its own `status`, is a `500`: guessing that a random
 * error's message is safe to forward is how internals leak.
 *
 * One detail about the log line. `pino-http` decides what to report from
 * `res.err`, substituting a synthetic "failed with status code 500" error when
 * nothing is set. So the handler assigns the real error to `res.err`, and the
 * single per-request line then carries the actual failure with its stack instead
 * of a placeholder.
 */

import type { ErrorRequestHandler, NextFunction, Request, RequestHandler, Response } from 'express';

import {
  INTERNAL_ERROR,
  NOT_FOUND,
  PAYLOAD_TOO_LARGE,
  VALIDATION_FAILED,
  sendError,
} from './errors.js';
import { JSON_BODY_LIMIT } from './security.js';

/**
 * The only message a client ever sees for an unhandled error. Fixed text, so
 * there is no path by which an error string reaches the response body.
 */
export const INTERNAL_ERROR_MESSAGE = 'internal server error';

/**
 * `body-parser` failure types that are the client's fault, mapped to the status
 * and message they get. Everything else it can throw (`entity.verify.failed`,
 * `stream.encoding.set`, and friends) is a server-side problem and falls through
 * to the 500.
 */
const BODY_ERRORS: Record<
  string,
  { readonly status: number; readonly code: string; readonly message: string }
> = {
  'entity.parse.failed': {
    status: 400,
    code: VALIDATION_FAILED,
    message: 'request body must be valid JSON',
  },
  'entity.too.large': {
    status: 413,
    code: PAYLOAD_TOO_LARGE,
    message: `request body must not exceed ${JSON_BODY_LIMIT}`,
  },
  'encoding.unsupported': {
    status: 400,
    code: VALIDATION_FAILED,
    message: 'request body uses an unsupported content encoding',
  },
  'charset.unsupported': {
    status: 400,
    code: VALIDATION_FAILED,
    message: 'request body uses an unsupported charset',
  },
};

/**
 * Anything can be thrown in JavaScript, and a bare string carries no stack and no
 * structure for a log serializer to work with. Wrapping it gives the log line both,
 * and satisfies `res.err`, which `pino-http` types as an `Error`.
 */
function toError(value: unknown): Error {
  return value instanceof Error ? value : new Error(`non-Error thrown: ${String(value)}`);
}

function bodyErrorType(error: unknown): string | null {
  if (typeof error !== 'object' || error === null || !('type' in error)) {
    return null;
  }
  const { type } = error as { type?: unknown };
  return typeof type === 'string' ? type : null;
}

/**
 * Unmatched route: `404` in the standard envelope.
 *
 * Without this, Express's default handler answers with an HTML page, which a
 * client that only knows how to parse the error envelope cannot read. The message
 * never echoes the requested path, so the API cannot be used to reflect arbitrary
 * text back to a browser.
 */
export function createNotFoundHandler(): RequestHandler {
  return (req: Request, res: Response) => {
    // Info, not warn: a wrong path is a client mistake with no service impact,
    // and the request log line already records the 404 with its path.
    req.log.info('no route matched');
    sendError(req, res, 404, NOT_FOUND, 'resource not found');
  };
}

/**
 * The central error handler. Mount it last, after every router and after the
 * 404 handler.
 */
export function createErrorHandler(): ErrorRequestHandler {
  return (thrown: unknown, req: Request, res: Response, next: NextFunction) => {
    const error = toError(thrown);
    // Give the request logger the real error rather than its placeholder.
    res.err = error;

    if (res.headersSent) {
      // The status line is already on the wire, so there is no envelope left to
      // send. Handing it back to Express is the only correct move: it destroys
      // the socket, which is how a client learns the response is truncated
      // rather than complete.
      req.log.error({ err: error }, 'error thrown after the response had started');
      next(error);
      return;
    }

    const mapped = BODY_ERRORS[bodyErrorType(thrown) ?? ''];
    if (mapped !== undefined) {
      // Warn, not error: rejecting a bad or oversized body is the middleware
      // doing its job.
      req.log.warn({ err: error }, 'request body rejected');
      sendError(req, res, mapped.status, mapped.code, mapped.message);
      return;
    }

    // The full error — message, stack, cause — goes to the log and stops there.
    // The per-request line carries it as well, through the `res.err` assignment
    // above; this line is the one that says what the client was told instead.
    req.log.error({ err: error }, 'unhandled error, responding 500');
    sendError(req, res, 500, INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE);
  };
}
