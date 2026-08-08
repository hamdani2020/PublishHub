/**
 * `GET /api/v1/posts` and `GET /api/v1/posts/:id` (Requirement 2.5).
 *
 * Both endpoints are pure reads over the post store, so the interesting decisions
 * are about bounds and about what "missing" means.
 *
 * **The list is always bounded.** `limit` defaults to
 * {@link DEFAULT_POSTS_LIMIT} (20 — enough to fill the web frontend's recent-posts
 * list without asking for the whole index) and is capped at
 * {@link RECENT_POSTS_MAX}, which is all the index holds anyway. A `limit` above
 * the cap is clamped rather than rejected: the client asked for "as many as you
 * have", and there is no honest error to report. A `limit` that is not a positive
 * integer *is* rejected, because it signals a broken client rather than an
 * ambitious one.
 *
 * **A missing record is a 404, not an empty 200.** An empty *list* is a 200 with
 * `{ "posts": [] }` — no posts is a valid state of the world, and a client
 * rendering a list should not have to special-case it.
 *
 * **A dangling index entry is skipped, not surfaced.** The store drops entries it
 * cannot read (see `decodePostRecord`), so the list can be shorter than `limit`
 * and never 500s over a hash that has gone away. The same rule makes a corrupt
 * single record read as a 404: the client's situation is identical either way, and
 * the log line carries the detail an operator needs.
 *
 * Responses are `no-store`. Post status changes seconds after submission, and a
 * cached "queued" is worse than a round trip.
 */

import { Router } from 'express';
import type { Request, Response } from 'express';

import { DEPENDENCY_UNAVAILABLE, NOT_FOUND, VALIDATION_FAILED, sendError } from '../http/index.js';
import { RECENT_POSTS_MAX } from './post-store.js';
import type { PostRecord, PostStore } from './post-store.js';

export const POSTS_PATH = '/api/v1/posts';
export const POST_PATH = '/api/v1/posts/:id';

/**
 * How many posts a request without a `limit` returns. Sized for the frontend's
 * recent-posts list (Requirement 4.5) rather than for the index cap: a default of
 * 100 would make the common call an order of magnitude more expensive than it
 * needs to be.
 */
export const DEFAULT_POSTS_LIMIT = 20;

/** The ceiling on `limit`. Higher values are clamped to it. */
export const MAX_POSTS_LIMIT = RECENT_POSTS_MAX;

/**
 * The list response. An object rather than a bare array: it leaves room to add
 * pagination fields later without breaking a client, and a top-level JSON array
 * is a shape best avoided in a public response.
 *
 * `limit` is echoed so a client can tell a clamped request from an exhausted one:
 * `posts.length < limit` means there are no more.
 */
export interface PostListBody {
  readonly posts: readonly PostRecord[];
  readonly count: number;
  /** The effective limit after clamping, not necessarily what was requested. */
  readonly limit: number;
}

export interface QueryRouterDeps {
  readonly store: Pick<PostStore, 'get' | 'listRecent'>;
}

type LimitResult = { readonly ok: true; readonly value: number } | { readonly ok: false };

/**
 * Parse `?limit=`. Absent is the default; a positive integer is clamped to the
 * cap; anything else fails.
 *
 * Express gives a repeated `?limit=1&limit=2` as an array. That is a client bug
 * with no sensible resolution, so it is rejected rather than silently resolved to
 * one of the two.
 */
export function parseLimit(raw: unknown): LimitResult {
  if (raw === undefined) {
    return { ok: true, value: DEFAULT_POSTS_LIMIT };
  }
  if (typeof raw !== 'string') {
    return { ok: false };
  }
  // A strict integer test, so `3.5`, `1e2`, `0x10`, `+5`, and ` 5 ` all fail
  // rather than landing on whatever `Number` makes of them.
  if (!/^\d+$/.test(raw)) {
    return { ok: false };
  }
  const parsed = Number(raw);
  if (parsed < 1) {
    return { ok: false };
  }
  return { ok: true, value: Math.min(parsed, MAX_POSTS_LIMIT) };
}

export function createQueryRouter(deps: QueryRouterDeps): Router {
  const { store } = deps;
  const router = Router();

  router.get(POSTS_PATH, (req: Request, res: Response) => {
    // Express 4 does not catch rejections from an async handler; both handlers
    // resolve in every case, so voiding the promise is safe.
    void handleList(req, res);
  });

  router.get(POST_PATH, (req: Request, res: Response) => {
    void handleGet(req, res);
  });

  async function handleList(req: Request, res: Response): Promise<void> {
    const limit = parseLimit(req.query.limit);
    if (!limit.ok) {
      req.log.warn('posts list rejected: invalid limit');
      sendError(
        req,
        res,
        400,
        VALIDATION_FAILED,
        `limit must be a positive integer of at most ${String(MAX_POSTS_LIMIT)}`,
      );
      return;
    }

    let posts: PostRecord[];
    try {
      posts = await store.listRecent(limit.value);
    } catch (error) {
      req.log.error({ err: error }, 'failed to read recent posts');
      sendError(req, res, 503, DEPENDENCY_UNAVAILABLE, 'post store unavailable');
      return;
    }

    const body: PostListBody = { posts, count: posts.length, limit: limit.value };
    res.set('cache-control', 'no-store');
    res.status(200).json(body);
  }

  async function handleGet(req: Request, res: Response): Promise<void> {
    // Express only matches this route with a non-empty `:id` segment, so the
    // fallback is unreachable; it is written to fail closed (an empty id reads as
    // absent, giving a 404) rather than asserted away.
    const postId = req.params.id ?? '';

    let post: PostRecord | null;
    try {
      post = await store.get(postId);
    } catch (error) {
      req.log.error({ err: error, post_id: postId }, 'failed to read post record');
      sendError(req, res, 503, DEPENDENCY_UNAVAILABLE, 'post store unavailable');
      return;
    }

    if (post === null) {
      // Info, not warn: an unknown id is a normal outcome of a client polling an
      // id it never had. The request logger already records the 404.
      req.log.info({ post_id: postId }, 'post not found');
      sendError(req, res, 404, NOT_FOUND, 'post not found');
      return;
    }

    res.set('cache-control', 'no-store');
    res.status(200).json(post);
  }

  return router;
}
