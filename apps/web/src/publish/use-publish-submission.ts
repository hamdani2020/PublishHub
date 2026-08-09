/**
 * Submission state for the composer (Requirements 4.2, 4.3, 4.4).
 *
 * Four states, one at a time: `idle`, `pending`, `queued`, `error`. A single
 * discriminated union rather than the usual `isLoading`/`error`/`data` trio,
 * because that trio can represent things that cannot happen — pending and
 * errored at once, a result alongside a failure — and every consumer then has
 * to decide which field wins.
 *
 * Duplicate submissions are blocked by a ref, not by the state. `state` is the
 * render-time truth and React may batch it, so two clicks inside one batch
 * would both see `idle`. The ref flips synchronously inside the handler, which
 * is where the second click has to be stopped (Requirement 4.4). The disabled
 * button is the visible half of the same guarantee; this is the half that holds
 * when a submit arrives from somewhere other than the button.
 *
 * The hook never touches the draft. That belongs to `ComposerForm`, which does
 * not clear it, so a failed submission keeps the user's content by construction
 * rather than by an explicit restore step (Requirement 4.3).
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import type { ComposerDraft } from '../composer';
import { submitPost } from './publish-client';
import type { PublishOutcome } from './publish-client';

export interface IdleState {
  readonly status: 'idle';
}

export interface PendingState {
  readonly status: 'pending';
}

export interface QueuedState {
  readonly status: 'queued';
  readonly id: string;
}

export interface ErrorState {
  readonly status: 'error';
  readonly message: string;
  readonly requestId: string | null;
}

export type SubmissionState = IdleState | PendingState | QueuedState | ErrorState;

export const IDLE_STATE: IdleState = Object.freeze({ status: 'idle' });

export interface UsePublishSubmissionOptions {
  /** Resolved `RuntimeConfig.apiBaseUrl`. */
  readonly apiBaseUrl: string;
  /** Injection seam for tests; defaults to the global `fetch`. */
  readonly fetchImpl?: typeof fetch | undefined;
}

export interface PublishSubmission {
  readonly state: SubmissionState;
  /** `state.status === 'pending'`, named so callers do not re-derive it. */
  readonly pending: boolean;
  /**
   * Submit a validated draft. Returns immediately and ignores the call while a
   * submission is already in flight.
   */
  readonly submit: (draft: ComposerDraft) => void;
}

export function usePublishSubmission(options: UsePublishSubmissionOptions): PublishSubmission {
  const { apiBaseUrl, fetchImpl } = options;
  const [state, setState] = useState<SubmissionState>(IDLE_STATE);

  const inFlight = useRef(false);
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

  const submit = useCallback(
    (draft: ComposerDraft): void => {
      if (inFlight.current) {
        return;
      }
      inFlight.current = true;
      setState({ status: 'pending' });

      // The handler stays synchronous — `ComposerForm.onSubmit` returns void and a
      // promise handed to it would be dropped — so the async work is started
      // explicitly and its rejection path is closed below.
      void (async () => {
        let outcome: PublishOutcome;
        try {
          outcome = await submitPost(draft, {
            apiBaseUrl,
            ...(fetchImpl === undefined ? {} : { fetchImpl }),
          });
        } catch (error) {
          // `submitPost` maps every network and protocol failure itself, so this
          // is only reachable if the client throws for a reason we did not
          // anticipate. Reporting it as an error state beats an unhandled
          // rejection that leaves the button spinning forever.
          outcome = {
            kind: 'error',
            message: `Something went wrong before the post could be sent: ${errorSummary(error)}. Your draft has been kept, so you can publish again.`,
            requestId: null,
          };
        } finally {
          inFlight.current = false;
        }

        // A result arriving after unmount has nowhere to go; setting state here
        // is a leak warning at best and a lost update at worst.
        if (!mounted.current) {
          return;
        }

        // A fresh object every time, deliberately: the result region focuses on a
        // new state identity, so two identical outcomes in a row still announce.
        setState(
          outcome.kind === 'queued'
            ? { status: 'queued', id: outcome.id }
            : { status: 'error', message: outcome.message, requestId: outcome.requestId },
        );
      })();
    },
    [apiBaseUrl, fetchImpl],
  );

  return { state, pending: state.status === 'pending', submit };
}

/** A short, safe description. Never a stack, which a user cannot act on. */
function errorSummary(error: unknown): string {
  return error instanceof Error && error.message !== '' ? error.message : 'unexpected failure';
}
