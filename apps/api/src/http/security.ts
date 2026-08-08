/**
 * Security middleware: response headers, CORS, and the request body ceiling
 * (Requirement 2.9, design.md "Security posture").
 *
 * The API has no end-user authentication — that is a documented decision, not an
 * omission — so the controls that remain are the ones that limit what a browser
 * or a stranger on the network can do with it: a conservative set of response
 * headers, an origin allow-list, and a bound on how much body the process will
 * buffer.
 *
 * The wildcard rule is the part worth stating twice. `CORS_ORIGINS=*` is a local
 * convenience and never a deployed default, so `config.ts` refuses to load it
 * unless `NODE_ENV=development`, and {@link createCors} refuses to *emit* it
 * under the same condition. Two independent checks, because the failure mode of
 * the missing one is an open API rather than a broken build.
 */

import cors from 'cors';
import type { CorsOptions } from 'cors';
import helmet from 'helmet';
import type { RequestHandler } from 'express';

import type { ApiConfig } from '../config/index.js';
import { CORRELATION_ID_ALT_HEADER, CORRELATION_ID_HEADER } from '../logging/index.js';

/**
 * Maximum JSON request body, as a `body-parser` size string.
 *
 * Derived from the one body this service accepts rather than picked round:
 * `content` is capped at 5000 characters, and a pathological payload can spend
 * six bytes per character (`\u00e9`-style escapes) for 30 KB, plus the
 * `platforms` array and JSON punctuation. 64 KB leaves that worst case a
 * comfortable margin while keeping a hostile client from making the process
 * buffer megabytes before validation ever runs.
 *
 * Requests over the limit are rejected by `body-parser` before the handler sees
 * them; the central error handler turns that into a `413`.
 */
export const JSON_BODY_LIMIT = '64kb';

/**
 * How long a browser may cache a preflight result. Ten minutes: long enough that
 * a session does not re-preflight every request, short enough that an
 * allow-list change takes effect without waiting out a day-long cache.
 */
const PREFLIGHT_MAX_AGE_SECONDS = 600;

/**
 * Default security headers.
 *
 * `helmet`'s defaults are kept wholesale, including the restrictive
 * `Content-Security-Policy`. This service only ever answers with JSON, so a
 * policy written for documents costs nothing and still matters: it is what makes
 * a browser refuse to execute a response that somehow gets rendered as HTML.
 * `X-Content-Type-Options: nosniff` is the header that carries the most weight
 * for a JSON API — it stops a browser from guessing a different content type
 * than the one declared.
 */
export function createSecurityHeaders(): RequestHandler {
  return helmet();
}

/**
 * The origins allowed to make browser requests, after the development-only
 * wildcard is filtered out.
 *
 * Returning `'*'` is possible only in development. Outside it, a configuration
 * that somehow reached this point with a wildcard loses it, and an allow-list
 * left empty by that filtering denies every origin rather than falling open.
 */
export function resolveAllowedOrigins(config: ApiConfig): '*' | readonly string[] {
  if (config.allowAnyOrigin && config.nodeEnv === 'development') {
    return '*';
  }
  return config.corsOrigins.filter((origin) => origin !== '*');
}

/**
 * CORS from the configured allow-list.
 *
 * A request from a disallowed origin is *not* rejected: it is answered without
 * an `Access-Control-Allow-Origin` header, and the browser refuses to hand the
 * response to the page. That is how CORS is specified to work, and returning a
 * 403 instead would break every non-browser client — `curl`, the worker, a probe
 * — none of which send an `Origin` at all.
 *
 * Credentials stay off. There is no session to carry, and an allow-list plus
 * credentials is the combination that turns a permissive origin entry into a
 * usable attack.
 */
export function createCors(config: ApiConfig): RequestHandler {
  const allowed = resolveAllowedOrigins(config);
  const options: CorsOptions = {
    // An empty allow-list stays an empty array rather than becoming `undefined`,
    // which `cors` would read as "allow everything".
    origin: allowed === '*' ? '*' : [...allowed],
    // Only what the API actually serves. OPTIONS is implicit in the preflight.
    methods: ['GET', 'POST', 'OPTIONS'],
    allowedHeaders: ['content-type', CORRELATION_ID_HEADER, CORRELATION_ID_ALT_HEADER],
    // So a browser client can read the id back and quote it in a bug report.
    exposedHeaders: [CORRELATION_ID_HEADER],
    credentials: false,
    maxAge: PREFLIGHT_MAX_AGE_SECONDS,
  };
  return cors(options);
}
