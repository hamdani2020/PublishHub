/**
 * The read side of the API: `GET /api/v1/posts` (Requirement 4.5).
 *
 * The contract, restated from `apps/api/src/posts/query-router.ts` and
 * `post-store.ts`:
 *
 * - `200 { "posts": [...], "count": n, "limit": n }`, newest first. An empty
 *   list is a normal `200`, not a 404.
 * - each post is a full record: `id`, `content`, `platforms`, `status`,
 *   `job_id`, `created_at`, `updated_at`.
 * - failures use the shared envelope, `{ "error": { code, message, request_id } }`,
 *   with `VALIDATION_FAILED` (400) for a bad `limit` and
 *   `DEPENDENCY_UNAVAILABLE` (503) when the store cannot be read.
 *
 * Like `../publish/publish-client`, this module never throws and never rejects:
 * every status, every parse failure, and every network error settles into one
 * discriminated union so the list has exactly one thing to render. The messages
 * are written for someone looking at an empty panel and wondering whether it is
 * broken or simply empty, so each one says which.
 *
 * Only the fields the list renders are kept. `job_id` and the timestamps are read
 * past rather than mapped: the record's ordering already comes from the API, and
 * a field nothing displays is a field that can drift unnoticed.
 */

/** Appended to the configured base URL, which never carries a trailing slash. */
export const POSTS_PATH = '/v1/posts';

/**
 * How many posts the panel asks for. Well under the API's own default of 20 and
 * its cap of 100: this is a "recent posts" list, and a hundred rows of other
 * people's drafts is not more useful than ten.
 */
export const RECENT_POSTS_LIMIT = 10;

/**
 * One post as the list needs it.
 *
 * `platforms` and `status` are plain strings rather than the narrow unions the
 * API uses internally. The worker owns the status vocabulary and can grow it
 * (spec task 4.2 writes four of the five), and a frontend that rejected an
 * unrecognised value would make posts disappear from the list on the day a new
 * status ships. Display handles the unknown case instead — see `post-display`.
 */
export interface PostSummary {
  readonly id: string;
  /** The full submitted content. Truncation is a display concern, not a wire one. */
  readonly content: string;
  readonly platforms: readonly string[];
  readonly status: string;
}

export interface PostsLoaded {
  readonly kind: 'posts';
  /** Newest first, as the API returned them. Possibly empty, which is valid. */
  readonly posts: readonly PostSummary[];
}

export interface PostsFailed {
  readonly kind: 'error';
  /** Actionable, human-readable, safe to render. Never a raw server string. */
  readonly message: string;
  /** The envelope's `request_id`, for a user to quote. `null` when unknown. */
  readonly requestId: string | null;
}

export type PostsOutcome = PostsLoaded | PostsFailed;

export interface PostsClientOptions {
  /** Resolved `RuntimeConfig.apiBaseUrl`: `/api`, `''`, or an absolute origin. */
  readonly apiBaseUrl: string;
  /** Injection seam for tests; read lazily from the global by default. */
  readonly fetchImpl?: typeof fetch | undefined;
  /** Defaults to {@link RECENT_POSTS_LIMIT}. */
  readonly limit?: number | undefined;
  /** Lets a caller cancel a refresh that has been superseded. */
  readonly signal?: AbortSignal | undefined;
}

export function postsUrl(apiBaseUrl: string, limit: number = RECENT_POSTS_LIMIT): string {
  return `${apiBaseUrl}${POSTS_PATH}?limit=${String(limit)}`;
}

/**
 * Fetch the recent posts.
 *
 * Resolves with `posts` or `error`. Rejects only if an `AbortSignal` was supplied
 * and fired, which is the caller's own doing.
 */
export async function fetchRecentPosts(options: PostsClientOptions): Promise<PostsOutcome> {
  const url = postsUrl(options.apiBaseUrl, options.limit ?? RECENT_POSTS_LIMIT);
  const doFetch = options.fetchImpl ?? globalThis.fetch.bind(globalThis);

  let response: Response;
  try {
    response = await doFetch(url, {
      method: 'GET',
      headers: { accept: 'application/json' },
      ...(options.signal === undefined ? {} : { signal: options.signal }),
    });
  } catch (error) {
    if (options.signal?.aborted === true) {
      // Superseded by a newer refresh, or unmounted. Not a failure to report.
      throw error;
    }
    return {
      kind: 'error',
      message: `Could not reach the API at ${describeBase(options.apiBaseUrl)}, so recent posts are unavailable. Check that the API is running, then refresh.`,
      requestId: null,
    };
  }

  const body = await readJson(response);

  if (!response.ok) {
    return failureOutcome(response, body);
  }

  const posts = readPosts(body);
  if (posts === null) {
    return {
      kind: 'error',
      message: 'The API returned recent posts in a shape this page does not understand. Refresh to try again, and report this if it persists.',
      requestId: readEnvelopeRequestId(body),
    };
  }
  return { kind: 'posts', posts };
}

function failureOutcome(response: Response, body: unknown): PostsFailed {
  const requestId = readEnvelopeRequestId(body);
  const status = response.status;

  if (status === 404) {
    // Almost always a misconfigured base URL or a proxy that is not routing
    // `/api`, so the message points at configuration rather than at retrying.
    return {
      kind: 'error',
      message: 'The recent posts endpoint was not found. The API base URL is probably misconfigured; report this rather than refreshing.',
      requestId,
    };
  }

  if (status === 503) {
    return {
      kind: 'error',
      message: 'Recent posts are temporarily unavailable: the API cannot reach the store that holds them. Posting still works. Refresh in a moment.',
      requestId,
    };
  }

  if (status >= 500) {
    return {
      kind: 'error',
      message: `The API failed to return recent posts (server error ${String(status)}). Refresh to try again, and report the reference below if it keeps failing.`,
      requestId,
    };
  }

  // 400 lands here. It means this client built a `limit` the API rejects, which
  // is our bug and not something the reader can fix by retrying — so the message
  // says so rather than inviting a pointless refresh.
  return {
    kind: 'error',
    message: `The API refused the recent posts request (status ${String(status)}). This looks like a bug in this page rather than something you can fix; report the reference below.`,
    requestId,
  };
}

/**
 * Read `posts` out of the list body.
 *
 * Returns `null` only when the envelope itself is unusable — a non-object body,
 * or `posts` missing or not an array — because that is the case where the page
 * genuinely cannot tell an empty list from a broken response.
 *
 * Individual entries that do not decode are *skipped*, mirroring the API's own
 * forgiving read path: it already drops index entries it cannot resolve, and one
 * unreadable record is not a reason to blank the whole panel.
 */
function readPosts(body: unknown): readonly PostSummary[] | null {
  const raw = readField(body, 'posts');
  if (!Array.isArray(raw)) {
    return null;
  }
  return raw
    .map((entry) => readPost(entry))
    .filter((post): post is PostSummary => post !== null);
}

function readPost(entry: unknown): PostSummary | null {
  const id = readString(entry, 'id');
  const status = readString(entry, 'status');
  const content = readField(entry, 'content');
  const platforms = readField(entry, 'platforms');

  // `content` may legitimately be any string the user submitted, but it must be
  // a string: a number or an object here means we are not looking at a record.
  if (id === null || status === null || typeof content !== 'string' || !Array.isArray(platforms)) {
    return null;
  }

  return {
    id,
    content,
    // A record always carries at least one platform; a stray non-string entry is
    // dropped rather than rendered as `undefined`.
    platforms: platforms.filter((value): value is string => typeof value === 'string' && value !== ''),
    status,
  };
}

/**
 * Parse the body, or return `undefined`.
 *
 * An HTML error page from a proxy, an empty 502, and a truncated response all
 * mean "no envelope", and the status-based message is still the right thing to
 * show.
 */
async function readJson(response: Response): Promise<unknown> {
  try {
    return (await response.json()) as unknown;
  } catch {
    return undefined;
  }
}

function readEnvelopeRequestId(body: unknown): string | null {
  return readString(readField(body, 'error'), 'request_id');
}

function readField(value: unknown, key: string): unknown {
  if (typeof value !== 'object' || value === null) {
    return undefined;
  }
  return (value as Record<string, unknown>)[key];
}

/** A present, non-blank string at `key`, or `null`. Blank is as useless as absent. */
function readString(value: unknown, key: string): string | null {
  const found = readField(value, key);
  if (typeof found !== 'string' || found.trim() === '') {
    return null;
  }
  return found;
}

/** An empty base means "this origin", which is not something to render as nothing. */
function describeBase(apiBaseUrl: string): string {
  return apiBaseUrl === '' ? 'this site' : apiBaseUrl;
}
