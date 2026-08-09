/**
 * The recent posts panel (Requirements 4.5, 4.6).
 *
 * Presentational on purpose: the state and the refresh come from
 * `useRecentPosts` in the caller, exactly as `SubmissionStatus` takes the
 * submission state. That is what lets a test render the populated, empty, and
 * failed panels directly, without a network double for each.
 *
 * The accessibility decisions, since this is a data display rather than a form:
 *
 * - **A real `<ul>` of `<li>`**, so assistive technology announces "list, 3
 *   items" and the reader knows how much is there before walking it. A stack of
 *   `<div>`s would render identically and tell them nothing.
 * - **Each row is a `<dl>`**, so every value arrives with its label attached:
 *   "Status: Queued", not a bare "Queued" whose meaning depends on which column
 *   it happened to be in. The labels are visible text, so this is not a
 *   screen-reader-only affordance.
 * - **One small live region for the panel's state**, always present in the DOM
 *   and always describing the current state ("Loading…", "No posts yet…",
 *   "Showing 3 recent posts…", or the failure). A live region has to exist before
 *   its content changes, and wrapping the *list* in one instead would re-announce
 *   every row on every refresh.
 * - **Truncation is stated, not implied.** The ellipsis is a visual cue only, so
 *   a shortened preview also carries visually hidden text saying it was
 *   shortened — otherwise a screen reader reads a sentence that stops mid-thought
 *   with no explanation.
 *
 * Status is never carried by colour alone (WCAG 1.4.1): the tone class tints the
 * row's edge, and the word "Failed" or "Published" is right there in the text.
 */

import { useId } from 'react';

import { platformsLabel, previewContent, statusLabel, statusTone } from './post-display';
import type { PostSummary } from './posts-client';
import type { RecentPostsState } from './use-recent-posts';

export interface RecentPostsProps {
  readonly state: RecentPostsState;
  /** True while a reload is in flight over an already-settled state. */
  readonly refreshing?: boolean;
  /**
   * Called when the reader asks for a refresh. Optional: without it the panel is
   * read-only and the button is not rendered, rather than rendered inert.
   */
  readonly onRefresh?: (() => void) | undefined;
}

export function RecentPosts({ state, refreshing = false, onRefresh }: RecentPostsProps): JSX.Element {
  const headingId = `${useId()}-recent-posts`;
  const busy = state.status === 'loading' || refreshing;

  return (
    <section className="posts" aria-labelledby={headingId}>
      <div className="posts__header">
        <h2 className="posts__heading" id={headingId}>
          Recent posts
        </h2>
        {onRefresh !== undefined && (
          <button
            className="posts__refresh"
            type="button"
            onClick={onRefresh}
            disabled={busy}
            aria-busy={busy}
          >
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
        )}
      </div>

      {/* Always rendered, so the region exists in the accessibility tree before
          its text changes. `:empty` never applies: every state has a sentence. */}
      <p className="posts__status" aria-live="polite">
        {summarize(state)}
      </p>

      {state.status === 'error' && state.requestId !== null && (
        <p className="posts__reference">
          Reference: <code>{state.requestId}</code>
        </p>
      )}

      {state.status === 'ready' && state.posts.length > 0 && (
        <ul className="posts__list" aria-busy={busy}>
          {state.posts.map((post) => (
            <PostRow key={post.id} post={post} />
          ))}
        </ul>
      )}
    </section>
  );
}

function PostRow({ post }: { readonly post: PostSummary }): JSX.Element {
  const preview = previewContent(post.content);

  return (
    <li className={`posts__item posts__item--${statusTone(post.status)}`}>
      <dl className="posts__fields">
        <div className="posts__field">
          <dt className="posts__term">Post id</dt>
          <dd className="posts__value">
            <code>{post.id}</code>
          </dd>
        </div>

        <div className="posts__field posts__field--content">
          <dt className="posts__term">Content</dt>
          <dd className="posts__value">
            {preview.text}
            {preview.truncated && <span className="visually-hidden"> (shortened for this list)</span>}
          </dd>
        </div>

        <div className="posts__field">
          <dt className="posts__term">Platforms</dt>
          <dd className="posts__value">{platformsLabel(post.platforms)}</dd>
        </div>

        <div className="posts__field">
          <dt className="posts__term">Status</dt>
          <dd className="posts__value posts__value--status">{statusLabel(post.status)}</dd>
        </div>
      </dl>
    </li>
  );
}

/**
 * The one sentence that describes the panel's current state.
 *
 * Doubles as the caption a sighted reader sees and as what the live region
 * announces, so there is no second copy of this wording to keep in step.
 */
function summarize(state: RecentPostsState): string {
  if (state.status === 'loading') {
    return 'Loading recent posts…';
  }
  if (state.status === 'error') {
    return state.message;
  }
  const count = state.posts.length;
  if (count === 0) {
    return 'No posts yet. Publish a post and it will appear here.';
  }
  return `Showing ${String(count)} recent ${count === 1 ? 'post' : 'posts'}, newest first.`;
}
