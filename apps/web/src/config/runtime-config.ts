/**
 * Runtime configuration for the web frontend (Requirement 4.7).
 *
 * The frontend is a static bundle, so anything environment-specific has to
 * arrive after the build. `index.html` loads `/config.js`, which sets
 * `window.__PUBLISHHUB_CONFIG__`; the container entrypoint rewrites that file
 * from environment variables at start-up (spec task 7.2). One image, every
 * environment, no rebuild.
 *
 * Two consequences shape this module:
 *
 * 1. The value is untrusted input. It is written by a shell script into a
 *    global, which means "an object with the right fields" is a hope, not a
 *    guarantee. Every field is validated before use, and anything unusable is
 *    replaced by the documented default rather than allowed to surface as
 *    `undefined` in a fetch URL.
 * 2. The fallback is a real configuration, not an error path. `/api` is correct
 *    in development (the Vite dev server proxies it) and in the container
 *    (nginx proxies it to the API Service), so a missing `config.js` degrades to
 *    a working same-origin app instead of a blank screen. The resolution still
 *    reports what happened so the caller can log it.
 *
 * Unlike the API's config module this one never throws. A browser has nowhere
 * useful to fail fast to: refusing to render would turn a recoverable
 * misconfiguration into a blank page.
 */

declare global {
  // Deliberately `unknown`: the value comes from a generated script, so it is
  // validated here rather than trusted through a convenient type assertion.
  interface Window {
    __PUBLISHHUB_CONFIG__?: unknown;
  }
}

/** Matches the `API_BASE_URL` default in the design's configuration reference. */
export const DEFAULT_API_BASE_URL = '/api';

export interface RuntimeConfig {
  /**
   * Base URL every API request is built on, with no trailing slash, so callers
   * can append a rooted path: `${apiBaseUrl}/v1/posts`.
   *
   * Either a root-relative path (`/api`, the same-origin default) or an
   * absolute http(s) origin with optional path (`https://api.example.com`).
   */
  readonly apiBaseUrl: string;
}

export const FALLBACK_CONFIG: RuntimeConfig = Object.freeze({
  apiBaseUrl: DEFAULT_API_BASE_URL,
});

export interface RuntimeConfigResolution {
  readonly config: RuntimeConfig;
  /**
   * `runtime` when every value came from `window.__PUBLISHHUB_CONFIG__`,
   * `fallback` when a default had to stand in for a missing or invalid one.
   */
  readonly source: 'runtime' | 'fallback';
  /**
   * Human-readable reason the fallback was used, for a single console warning at
   * boot. `null` when the runtime value was accepted as-is.
   */
  readonly problem: string | null;
}

/**
 * Validate a candidate runtime configuration.
 *
 * Accepts the raw `window.__PUBLISHHUB_CONFIG__` value (or anything else, which
 * is why the parameter is `unknown`) and always returns a usable config.
 */
export function resolveRuntimeConfig(candidate: unknown): RuntimeConfigResolution {
  if (candidate === undefined || candidate === null) {
    return fallback('window.__PUBLISHHUB_CONFIG__ is not set');
  }
  if (typeof candidate !== 'object' || Array.isArray(candidate)) {
    return fallback(`window.__PUBLISHHUB_CONFIG__ must be an object, received ${describe(candidate)}`);
  }

  const raw = (candidate as { apiBaseUrl?: unknown }).apiBaseUrl;
  if (raw === undefined || raw === null || (typeof raw === 'string' && raw.trim() === '')) {
    return fallback('apiBaseUrl is missing');
  }
  if (typeof raw !== 'string') {
    return fallback(`apiBaseUrl must be a string, received ${describe(raw)}`);
  }

  const normalized = normalizeBaseUrl(raw.trim());
  if (normalized === null) {
    return fallback(`apiBaseUrl must be an http(s) URL or a path starting with /, received ${JSON.stringify(raw)}`);
  }

  return { config: { apiBaseUrl: normalized }, source: 'runtime', problem: null };
}

/**
 * Resolve the configuration from the given window. Defaults to the real one, so
 * application code calls `readRuntimeConfig()` and tests pass a stub.
 */
export function readRuntimeConfig(win: Pick<Window, '__PUBLISHHUB_CONFIG__'> | undefined = globalThis.window): RuntimeConfigResolution {
  // `window` is absent under a non-DOM test environment or SSR; the fallback
  // keeps that from being a crash.
  return resolveRuntimeConfig(win?.__PUBLISHHUB_CONFIG__);
}

/**
 * Strip a trailing slash and reject anything that is not a usable base URL.
 *
 * Returns `null` when the value is unusable. Rejecting non-http(s) schemes is
 * the point of the URL parse: `javascript:` or `data:` in a fetch base is a
 * misconfiguration worth ignoring, not a value to pass through.
 */
function normalizeBaseUrl(value: string): string | null {
  if (value.startsWith('/')) {
    // A root-relative base. `//host/path` is a protocol-relative URL, not a
    // path, and is rejected: the runtime config should be explicit about scheme.
    return value.startsWith('//') ? null : trimTrailingSlash(value);
  }

  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return null;
  }
  if (parsed.search !== '' || parsed.hash !== '') {
    // A query or fragment in a base URL would be silently dropped or duplicated
    // by every path we append to it.
    return null;
  }
  return trimTrailingSlash(parsed.origin + parsed.pathname);
}

/** `/api/` -> `/api`, and `/` -> `''` (same-origin root, nothing to prefix). */
function trimTrailingSlash(value: string): string {
  return value.endsWith('/') ? value.slice(0, -1) : value;
}

function fallback(problem: string): RuntimeConfigResolution {
  return { config: FALLBACK_CONFIG, source: 'fallback', problem };
}

function describe(value: unknown): string {
  return Array.isArray(value) ? 'an array' : typeof value;
}
