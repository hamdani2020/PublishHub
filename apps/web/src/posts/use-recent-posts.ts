/**
 * Recent-posts state for the list panel (Requirement 4.5).
 *
 * Three states, one at a time: `loading`, `ready`, `error` — the same
 * discriminated-union shape as `../publish/use-publish-submission`, for the same
 * reason: an `isLoading`/`error`/`data` trio can represent things that cannot
 * happen, and every consumer then has to decide which field wins.
 *
 * Two details that are easy to get wrong in a panel that refreshes:
 *
 * - **A refresh does not blank the list.** `reload` keeps the current state until
 *   the new response lands and reports the in-flight refresh through `refreshing`
 *   instead. Dropping back to `loading` would make the rows disappear and
 *   reappear on every publish, which reads as a bug and moves everything below
 *   them.
 * - **Only the newest request may write state.** Each load takes a generation
 *   number and a stale response is discarded, so two refreshes in quick
 *   succession cannot land out of order and leave the older list showing. The
 *   same counter is what makes the unmount guard sufficient without an
 *   `AbortController`: the request is allowed to finish, its result is simply
 *   ignored.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { fetchRecentPosts } from './posts-client';
import type { PostSummary, PostsOutcome } from './posts-client';

export interface LoadingPostsState {
  readonly status: 'loading';
}

export interface ReadyPostsState {
  readonly status: 'ready';
  /** Newest first. Empty is a valid, expected state, not an error. */
  readonly posts: readonly PostSummary[];
}

export interface ErrorPostsState {
  readonly status: 'error';
  readonly message: string;
  readonly requestId: string | null;
}

export type RecentPostsState = LoadingPostsState | ReadyPostsState | ErrorPostsState;

export const LOADING_POSTS_STATE: LoadingPostsState = Object.freeze({ status: 'loading' });

export interface UseRecentPostsOptions {
  /** Resolved `RuntimeConfig.apiBaseUrl`. */
  readonly apiBaseUrl: string;
  /** Injection seam for tests; defaults to the global `fetch`. */
  readonly fetchImpl?: typeof fetch | undefined;
  /** Defaults to the client's `RECENT_POSTS_LIMIT`. */
  readonly limit?: number | undefined;
}

/** What the hook hands back. Named for the result, not the data, so it does not
 *  collide with the `RecentPosts` component that renders it. */
export interface RecentPostsResult {
  readonly state: RecentPostsState;
  /** True while a reload is in flight over an already-settled state. */
  readonly refreshing: boolean;
  /** Fetch again, keeping what is on screen until the answer arrives. */
  readonly reload: () => void;
}

export function useRecentPosts(options: UseRecentPostsOptions): RecentPostsResult {
  const { apiBaseUrl, fetchImpl, limit } = options;
  const [state, setState] = useState<RecentPostsState>(LOADING_POSTS_STATE);
  const [refreshing, setRefreshing] = useState(false);

  /** Bumped per load; only the newest generation may write state. */
  const generation = useRef(0);
  const mounted = useRef(true);

  useEffect(() => {
    // Assigned in the body as well as the cleanup: React's development
    // double-invocation runs the cleanup once before the real mount, and a ref
    // left at `false` there would swallow every later result.
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  /**
   * Start a load. Deliberately contains no synchronous `setState`: it is called
   * from an effect for the initial fetch, and a state update in an effect body
   * costs a cascading render (and is what `react-hooks/set-state-in-effect`
   * exists to catch). The `refreshing` flag is set by `reload`, which is called
   * from an event handler where a synchronous update is exactly right.
   */
  const load = useCallback(
    (): void => {
      generation.current += 1;
      const current = generation.current;

      void (async () => {
        let outcome: PostsOutcome;
        try {
          outcome = await fetchRecentPosts({
            apiBaseUrl,
            ...(fetchImpl === undefined ? {} : { fetchImpl }),
            ...(limit === undefined ? {} : { limit }),
          });
        } catch (error) {
          // `fetchRecentPosts` maps every network and protocol failure itself, so
          // this is only reachable if it throws for a reason we did not
          // anticipate. Reporting it beats an unhandled rejection that leaves the
          // panel spinning forever.
          outcome = {
            kind: 'error',
            message: `Recent posts could not be loaded: ${errorSummary(error)}. Refresh to try again.`,
            requestId: null,
          };
        }

        // Superseded, or unmounted: the result has nowhere to go.
        if (!mounted.current || generation.current !== current) {
          return;
        }

        setRefreshing(false);
        setState(
          outcome.kind === 'posts'
            ? { status: 'ready', posts: outcome.posts }
            : { status: 'error', message: outcome.message, requestId: outcome.requestId },
        );
      })();
    },
    [apiBaseUrl, fetchImpl, limit],
  );

  // The initial load. Re-runs if the API base URL changes, which in practice it
  // never does — the config is resolved once at boot — but a hook that quietly
  // kept fetching the old host would be a trap for whoever changes that.
  useEffect(() => {
    load();
  }, [load]);

  const reload = useCallback((): void => {
    setRefreshing(true);
    load();
  }, [load]);

  return { state, refreshing, reload };
}

/** A short, safe description. Never a stack, which a user cannot act on. */
function errorSummary(error: unknown): string {
  return error instanceof Error && error.message !== '' ? error.message : 'unexpected failure';
}
