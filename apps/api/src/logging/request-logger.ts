/**
 * Request logging middleware (Requirement 2.6).
 *
 * Every handled request produces exactly one flat JSON line carrying `method`,
 * `path`, `status_code`, `duration_ms`, and `correlation_id`. Flat and not
 * nested on purpose: `req.method` and `res.statusCode` buried inside pino-http's
 * default objects are awkward to query and duplicate what the line already says.
 * `quietReqLogger` plus custom success and error objects give the shape above
 * and nothing else.
 *
 * The correlation id is reused from the incoming request when a proxy or client
 * supplied one, so a trace spans hops, and echoed back on the response so a
 * caller can quote it in a bug report. It is the same id the error envelope
 * reports as `request_id`.
 */

import { randomUUID } from 'node:crypto';
import type { IncomingMessage, ServerResponse } from 'node:http';

import { pinoHttp } from 'pino-http';
import type { HttpLogger } from 'pino-http';
import type { Logger } from 'pino';

/** Canonical correlation header, read on the way in and set on the way out. */
export const CORRELATION_ID_HEADER = 'x-request-id';
/** Also accepted inbound, because plenty of proxies emit this one instead. */
export const CORRELATION_ID_ALT_HEADER = 'x-correlation-id';

/**
 * A client-supplied id is untrusted input that ends up in every log line for the
 * request. Restricting it to short, printable, delimiter-free text keeps it from
 * smuggling newlines or quotes into the log stream or the response header.
 */
const SAFE_CORRELATION_ID = /^[A-Za-z0-9_.:-]{1,128}$/;

function headerValue(req: IncomingMessage, name: string): string | undefined {
  const raw = req.headers[name];
  if (typeof raw === 'string') {
    return raw.trim();
  }
  if (Array.isArray(raw)) {
    return raw[0]?.trim();
  }
  return undefined;
}

/**
 * Reuse a caller's correlation id when it is present and well-formed, otherwise
 * mint one. Never throws: an unusable inbound value is replaced, not rejected,
 * because a malformed header is not worth failing a request over.
 */
export function resolveCorrelationId(req: IncomingMessage): string {
  const candidate =
    headerValue(req, CORRELATION_ID_HEADER) ?? headerValue(req, CORRELATION_ID_ALT_HEADER);
  return candidate !== undefined && SAFE_CORRELATION_ID.test(candidate) ? candidate : randomUUID();
}

/** Path without the query string: query values can carry user content. */
export function requestPath(url: string | undefined): string {
  if (url === undefined || url === '') {
    return '/';
  }
  const cut = url.indexOf('?');
  return cut === -1 ? url : url.slice(0, cut);
}

/**
 * pino-http hands the custom-object hooks its own accumulated value, typed as
 * `any` by its declarations. Read the one field needed from it defensively
 * rather than trusting the shape.
 */
function durationMs(value: unknown): number {
  if (typeof value === 'object' && value !== null && 'duration_ms' in value) {
    const { duration_ms: raw } = value;
    if (typeof raw === 'number' && Number.isFinite(raw)) {
      return raw;
    }
  }
  return 0;
}

export interface RequestLogFields {
  readonly method: string;
  readonly path: string;
  readonly status_code: number;
  readonly duration_ms: number;
}

function requestFields(
  req: IncomingMessage,
  res: ServerResponse,
  value: unknown,
): RequestLogFields {
  return {
    method: req.method ?? 'UNKNOWN',
    path: requestPath(req.url),
    status_code: res.statusCode,
    duration_ms: durationMs(value),
  };
}

/**
 * Build the middleware. Mount it before the routes so `req.log` is available to
 * handlers and so aborted requests are still logged.
 */
export function createRequestLogger(logger: Logger): HttpLogger {
  return pinoHttp({
    logger,
    // Bind the correlation id to the child logger instead of the whole
    // serialized request object. Both flags are needed: without the response one
    // pino-http still attaches a serialized `req` to the completion line, which
    // is where the duplication would show up.
    quietReqLogger: true,
    quietResLogger: true,
    customAttributeKeys: { reqId: 'correlation_id', responseTime: 'duration_ms' },
    genReqId: (req, res) => {
      const id = resolveCorrelationId(req);
      if (!res.headersSent) {
        res.setHeader(CORRELATION_ID_HEADER, id);
      }
      return id;
    },
    customLogLevel: (_req, res, error) => {
      if (error !== undefined || res.statusCode >= 500) {
        return 'error';
      }
      return res.statusCode >= 400 ? 'warn' : 'info';
    },
    customSuccessObject: (req: IncomingMessage, res: ServerResponse, value: unknown) =>
      requestFields(req, res, value),
    // The full error stays server-side here; the client gets the generic
    // envelope from the error handler (Requirement 2.7).
    customErrorObject: (req: IncomingMessage, res: ServerResponse, error: Error, value: unknown) => ({
      ...requestFields(req, res, value),
      err: error,
    }),
  });
}
