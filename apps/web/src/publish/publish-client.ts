/**
 * The one network call the composer makes: `POST /api/v1/publish`
 * (Requirements 4.2, 4.3).
 *
 * The API's contract, restated from `apps/api/src/posts/publish-router.ts` and
 * `apps/api/src/http/errors.ts`:
 *
 * - `202 { "id": "...", "status": "queued" }` on acceptance.
 * - any non-2xx: `{ "error": { "code", "message", "request_id" } }`, where the
 *   codes that reach this client are `VALIDATION_FAILED` (400),
 *   `PAYLOAD_TOO_LARGE` (413), `QUEUE_UNAVAILABLE` and `DEPENDENCY_UNAVAILABLE`
 *   (503), `NOT_FOUND` (404), and `INTERNAL_ERROR` (500).
 *
 * This module's whole job is to turn all of that — plus the cases where there is
 * no response at all — into one settled value the UI can render. It never
 * throws and never rejects: a submit handler that has to reason about both a
 * result and an exception ends up with two error paths, and one of them is
 * always the one that rots.
 *
 * The messages are written for the person who is about to lose a draft, so each
 * one says what happened, whether the draft survived, and what to do next. The
 * server's `message` is folded in when it describes the user's own input (a
 * validation failure) and dropped when it describes our infrastructure — a
 * sentence about a queue is not actionable to someone writing a post. The
 * generic 500 message is never surfaced verbatim for the same reason: it is
 * fixed text meant for a log reader, and `request_id` is the part that actually
 * helps, which is why it is returned separately for the UI to show.
 */

import type { ComposerDraft } from '../composer';

/** Appended to the configured base URL, which never carries a trailing slash. */
export const PUBLISH_PATH = '/v1/publish';

/** Accepted: the post is queued, not published. `id` is the API's post id. */
export interface PublishQueued {
  readonly kind: 'queued';
  readonly id: string;
}

/**
 * Not accepted, for any reason: a rejected payload, an unreachable API, a
 * response we could not read. One shape, because the UI treats them the same
 * way — show the message, keep the draft.
 */
export interface PublishFailed {
  readonly kind: 'error';
  /** Actionable, human-readable, safe to render. Never a raw server string. */
  readonly message: string;
  /** The envelope's `request_id`, for a user to quote. `null` when unknown. */
  readonly requestId: string | null;
}

export type PublishOutcome = PublishQueued | PublishFailed;

export interface PublishClientOptions {
  /** Resolved `RuntimeConfig.apiBaseUrl`: `/api`, `''`, or an absolute origin. */
  readonly apiBaseUrl: string;
  /**
   * Injection seam for tests. Read lazily from the global by default so a test
   * that stubs `globalThis.fetch` after import still takes effect.
   */
  readonly fetchImpl?: typeof fetch | undefined;
  /** Lets a caller cancel in flight; unused today, free to support. */
  readonly signal?: AbortSignal | undefined;
}

export function publishUrl(apiBaseUrl: string): string {
  return `${apiBaseUrl}${PUBLISH_PATH}`;
}

/**
 * Submit a draft that has already passed client validation.
 *
 * Resolves with `queued` or `error`. Rejects only if an `AbortSignal` was
 * supplied and fired, which is the caller's own doing.
 */
export async function submitPost(
  draft: ComposerDraft,
  options: PublishClientOptions,
): Promise<PublishOutcome> {
  const url = publishUrl(options.apiBaseUrl);
  const doFetch = options.fetchImpl ?? globalThis.fetch.bind(globalThis);

  let response: Response;
  try {
    response = await doFetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      // The draft is already the payload shape the API expects; there is no
      // separate wire model to map to.
      body: JSON.stringify({ content: draft.content, platforms: draft.platforms }),
      ...(options.signal === undefined ? {} : { signal: options.signal }),
    });
  } catch (error) {
    if (options.signal?.aborted === true) {
      // The caller cancelled: hand the abort back rather than reporting a
      // failure the user did not cause.
      throw error;
    }
    // DNS failure, connection refused, TLS error, offline, CORS rejection — the
    // Fetch spec collapses them all into one opaque TypeError on purpose, so
    // there is nothing more specific to say than "we never reached it".
    return {
      kind: 'error',
      message: `Could not reach the API at ${describeBase(options.apiBaseUrl)}. Check your connection and that the API is running, then publish again. Your draft has been kept.`,
      requestId: null,
    };
  }

  const body = await readJson(response);

  if (response.ok) {
    return acceptedOutcome(response, body);
  }
  return failureOutcome(response, body);
}

/**
 * `202` is the documented success status, but any 2xx is treated as acceptance
 * as long as it carries an id: refusing a `200` from a future revision of the
 * API would fail a submission that actually succeeded, which is the worse error
 * — the post is queued either way and the user would publish it twice.
 */
function acceptedOutcome(response: Response, body: unknown): PublishOutcome {
  const id = readString(body, 'id');
  if (id !== null) {
    return { kind: 'queued', id };
  }
  return {
    kind: 'error',
    message: `The API answered ${String(response.status)} but did not return a post id, so the post may or may not be queued. Check the recent posts list before publishing again.`,
    requestId: readEnvelopeRequestId(body),
  };
}

function failureOutcome(response: Response, body: unknown): PublishFailed {
  const requestId = readEnvelopeRequestId(body);
  const serverMessage = readEnvelopeMessage(body);
  const status = response.status;

  if (status === 400 || status === 422) {
    // The server rejected the user's own input, so its message names the field
    // and the rule and is the most useful thing we can show. Reaching here at
    // all means client and server validation disagree — a client-side bug or a
    // drifted rule — so it must still read as something the user can act on.
    const detail = serverMessage === null ? '' : ` ${asSentence(serverMessage)}`;
    return {
      kind: 'error',
      message: `The API rejected this post.${detail} Adjust your draft and publish again.`,
      requestId,
    };
  }

  if (status === 413) {
    return {
      kind: 'error',
      message: 'This post is too large for the API to accept. Shorten the content and publish again.',
      requestId,
    };
  }

  if (status === 404) {
    // Almost always a misconfigured API base URL or a proxy that is not routing
    // /api, so the message points at configuration rather than at the draft.
    return {
      kind: 'error',
      message: `The publish endpoint was not found at ${publishUrl(currentBase(response))}. The API base URL is probably misconfigured; report this rather than retrying. Your draft has been kept.`,
      requestId,
    };
  }

  if (status === 429) {
    return {
      kind: 'error',
      message: 'The API is rate limiting requests. Wait a few seconds and publish again. Your draft has been kept.',
      requestId,
    };
  }

  if (status === 503) {
    // QUEUE_UNAVAILABLE or DEPENDENCY_UNAVAILABLE. The API guarantees nothing was
    // enqueued and no partial record was left behind, so retrying is safe and is
    // exactly what we should tell the user to do.
    return {
      kind: 'error',
      message: 'PublishHub is temporarily unable to accept posts: a service it depends on is unavailable. Nothing was queued, so wait a moment and publish again. Your draft has been kept.',
      requestId,
    };
  }

  if (status >= 500) {
    return {
      kind: 'error',
      message: `The API failed to handle this post (server error ${String(status)}). Your draft has been kept, so you can publish again; if it keeps failing, report the reference below.`,
      requestId,
    };
  }

  return {
    kind: 'error',
    message: `The API refused this post (status ${String(status)}). Your draft has been kept. Publish again, and report the reference below if it keeps failing.`,
    requestId,
  };
}

/**
 * Parse the body, or return `undefined`.
 *
 * A failure here is not an error path of its own: an HTML error page from a
 * proxy, an empty 502 body, and a truncated response all mean "no envelope",
 * and the status-based message above is still the right thing to show.
 */
async function readJson(response: Response): Promise<unknown> {
  try {
    return (await response.json()) as unknown;
  } catch {
    return undefined;
  }
}

function readEnvelopeMessage(body: unknown): string | null {
  return readString(readRecord(body, 'error'), 'message');
}

function readEnvelopeRequestId(body: unknown): string | null {
  return readString(readRecord(body, 'error'), 'request_id');
}

function readRecord(value: unknown, key: string): unknown {
  if (typeof value !== 'object' || value === null) {
    return undefined;
  }
  return (value as Record<string, unknown>)[key];
}

/** A present, non-blank string at `key`, or `null`. Blank is as useless as absent. */
function readString(value: unknown, key: string): string | null {
  const found = readRecord(value, key);
  if (typeof found !== 'string' || found.trim() === '') {
    return null;
  }
  return found;
}

/**
 * The base URL as it should be shown to a person. An empty base means "this
 * origin", which is not something to render as nothing.
 */
function describeBase(apiBaseUrl: string): string {
  return apiBaseUrl === '' ? 'this site' : apiBaseUrl;
}

/**
 * The base the response actually came from, so a 404 message names the URL that
 * was called rather than the one we think we configured. `Response.url` is empty
 * for a synthetic response, in which case there is nothing to trim.
 */
function currentBase(response: Response): string {
  const { url } = response;
  return url.endsWith(PUBLISH_PATH) ? url.slice(0, -PUBLISH_PATH.length) : url;
}

/** Server messages are lower-case fragments without terminal punctuation. */
function asSentence(message: string): string {
  const trimmed = message.trim();
  const capitalized = trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
  return /[.!?]$/.test(capitalized) ? capitalized : `${capitalized}.`;
}
