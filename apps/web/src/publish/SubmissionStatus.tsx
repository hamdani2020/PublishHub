/**
 * The submission result region (Requirements 4.2, 4.3, 4.6).
 *
 * Two things have to be true at once here, and they pull in opposite directions:
 * the outcome must be announced to a screen reader, and a keyboard user must end
 * up somewhere useful rather than back on a button whose meaning just changed.
 * The design calls for both, so both are implemented:
 *
 * - The **wrapper is always rendered** and always carries `aria-live="polite"`.
 *   A live region has to exist in the accessibility tree *before* its content
 *   changes; rendering the whole region only once there is a result is the
 *   classic way to build a live region that never announces anything.
 * - **Focus moves to the result** once it arrives. The result element takes
 *   `tabIndex={-1}` so it is programmatically focusable but not a tab stop, and
 *   sits before the form in DOM order, so tabbing on from it lands back in the
 *   composer — which is exactly where someone goes after a failure.
 *
 * The known cost: a screen reader may read the result twice, once from the live
 * region and once on focus. The alternative is worse in both directions — drop
 * the live region and a mouse user who never moves focus hears nothing; drop the
 * focus move and a keyboard user has to hunt for the message. Confirming which
 * combination reads best is manual screen-reader work, which the design already
 * notes is outside the checkable subset.
 *
 * Nothing renders while idle or pending. The pending state is shown on the
 * button itself, and clearing a stale confirmation as soon as a new submission
 * starts is the honest thing to do.
 */

import { useEffect, useRef } from 'react';

import type { SubmissionState } from './use-publish-submission';

export interface SubmissionStatusProps {
  readonly state: SubmissionState;
}

export function SubmissionStatus({ state }: SubmissionStatusProps): JSX.Element {
  const resultRef = useRef<HTMLDivElement>(null);

  const settled = state.status === 'queued' || state.status === 'error';

  useEffect(() => {
    if (settled) {
      resultRef.current?.focus();
    }
    // `state` is a new object per result, so two identical outcomes in a row
    // still move focus. Depending on `settled` alone would skip the second.
  }, [state, settled]);

  return (
    // `role="status"` on the always-present wrapper, with `aria-live` stated
    // explicitly as well: the role already implies a polite live region, and the
    // attribute is what the design names and what older assistive technology
    // honours most reliably. It also gives tests a role to query, so the region
    // cannot quietly lose its semantics.
    <div className="submission" role="status" aria-live="polite">
      {state.status === 'queued' && (
        <div className="submission__result submission__result--queued" ref={resultRef} tabIndex={-1}>
          <p className="submission__headline">Post queued</p>
          <p className="submission__detail">
            PublishHub accepted the post with id <code>{state.id}</code>. A worker will publish it to
            the selected platforms shortly.
          </p>
        </div>
      )}

      {state.status === 'error' && (
        <div className="submission__result submission__result--error" ref={resultRef} tabIndex={-1}>
          <p className="submission__headline">Post not queued</p>
          <p className="submission__detail">{state.message}</p>
          {state.requestId !== null && (
            <p className="submission__detail">
              Reference: <code>{state.requestId}</code>
            </p>
          )}
        </div>
      )}
    </div>
  );
}
