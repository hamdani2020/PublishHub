import { useEffect } from 'react';

import { ComposerForm } from './composer';
import type { RuntimeConfig } from './config';
import { RecentPosts, useRecentPosts } from './posts';
import { SubmissionStatus, usePublishSubmission } from './publish';

export interface AppProps {
  /**
   * Resolved runtime configuration, passed in rather than read from the global
   * inside the tree: components stay testable without touching `window`, and
   * there is exactly one place (`main.tsx`) that reads it.
   */
  readonly config: RuntimeConfig;
}

/**
 * Application shell.
 *
 * Holds the page skeleton — one `<h1>`, `<main>` as the landmark — and owns both
 * lifecycles: the composer collects and validates a draft, the
 * `usePublishSubmission` hook calls the API, and `SubmissionStatus` announces
 * whatever came back (Requirements 4.2, 4.3, 4.4), while `useRecentPosts` and
 * `RecentPosts` show what the platform has been asked to publish so far
 * (Requirement 4.5).
 *
 * The result region is rendered **before** the form on purpose. Focus moves there
 * when a submission settles, and from there the natural next Tab goes into the
 * composer — the right destination whether the post was queued (write another) or
 * rejected (fix this one). The posts panel comes last, after the form, because it
 * is a read: nobody should have to tab through ten rows to reach the textarea.
 */
export function App({ config }: AppProps): JSX.Element {
  const { state, pending, submit } = usePublishSubmission({ apiBaseUrl: config.apiBaseUrl });
  const { state: postsState, refreshing, reload } = useRecentPosts({ apiBaseUrl: config.apiBaseUrl });

  // A queued post is the one moment the list is certainly stale, so it refetches
  // then rather than on a timer: a poll would keep the API busy for a page nobody
  // is watching, and this is the change a reader is actually waiting to see.
  //
  // `state` is a fresh object per outcome, so two successful publishes in a row
  // trigger two refreshes; depending on the status string alone would skip the
  // second. The list is not updated locally from the response instead, because
  // the API is the authority on both the record and its position in the index —
  // a hand-built row would show `queued` even if the worker had already finished.
  useEffect(() => {
    if (state.status === 'queued') {
      reload();
    }
  }, [state, reload]);

  return (
    <div className="app">
      <header className="app__header">
        <h1>PublishHub</h1>
        <p className="app__tagline">Compose once, publish to every platform.</p>
      </header>

      <main className="app__main">
        <SubmissionStatus state={state} />
        <ComposerForm onSubmit={submit} pending={pending} />
        <RecentPosts state={postsState} refreshing={refreshing} onRefresh={reload} />
      </main>

      <footer className="app__footer">
        {/* Surfacing the resolved base URL makes a misconfigured deployment
            visible without opening the console. It is not a secret: the browser
            has to know it to call the API at all. */}
        <p>
          API base URL: <code>{config.apiBaseUrl === '' ? '/ (same origin)' : config.apiBaseUrl}</code>
        </p>
      </footer>
    </div>
  );
}
