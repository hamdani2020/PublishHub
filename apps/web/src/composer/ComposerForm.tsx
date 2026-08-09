import { useId, useRef, useState } from 'react';
import type { ChangeEvent, FormEvent } from 'react';

import {
  CONTENT_MAX_LENGTH,
  NO_ERRORS,
  PLATFORM_ALLOW_LIST,
  PLATFORM_LABELS,
  characterLength,
  hasErrors,
  validateDraft,
} from './publish-rules';
import type { ComposerDraft, DraftErrors, Platform } from './publish-rules';

export interface ComposerFormProps {
  /**
   * Called with a draft that has already passed client validation.
   *
   * The network call, the submission state, and the result announcement live in
   * the caller, not here. The form owns the draft and never clears it, which is
   * what keeps the user's content intact after a failed submission
   * (Requirement 4.3) without doing anything special.
   */
  readonly onSubmit: (draft: ComposerDraft) => void;
  /**
   * True while the caller's submission is in flight (Requirement 4.4).
   *
   * The form does not track this itself: only the caller knows when the request
   * settles. Passing it in keeps one source of truth instead of a local flag that
   * has to be told when to clear.
   */
  readonly pending?: boolean;
}

/**
 * The composer (Requirements 4.1, 4.6).
 *
 * A real `<form>` with real `<label>` elements, platforms as checkboxes inside a
 * `<fieldset>`/`<legend>` group, and validation messages tied to their control
 * with `aria-describedby`. Nothing here is a `<div>` pretending to be a control,
 * so keyboard operation, focus order, and the native Enter-to-submit behaviour
 * come from the browser rather than from handlers we would have to get right.
 *
 * Two decisions worth naming:
 *
 * - **Validation is on submit, then live.** Errors appear only after the first
 *   submit attempt, and from then on they re-evaluate on every keystroke, so a
 *   message disappears as soon as it stops being true. Validating from the first
 *   character would shout at someone who is still typing.
 * - **Errors are not `role="alert"`.** On a failed submit, focus moves to the
 *   first invalid control, and its `aria-describedby` already carries the
 *   message, so assistive technology reads it on arrival. An alert role on top of
 *   that announces the same sentence twice.
 * - **The pending state is the caller's, shown here.** `pending` disables the
 *   button, sets `aria-busy`, and changes its visible text; the handler also
 *   refuses to fire a second `onSubmit` while it is true. A real `disabled`
 *   attribute rather than `aria-disabled`, because the point is to make a second
 *   submission impossible and not merely discouraged; the cost is that a
 *   keyboard user loses focus to the document for the duration, which is why the
 *   caller moves focus to the result region as soon as the request settles.
 */
export function ComposerForm({ onSubmit, pending = false }: ComposerFormProps): JSX.Element {
  const [content, setContent] = useState('');
  const [platforms, setPlatforms] = useState<readonly Platform[]>([]);
  const [submitAttempted, setSubmitAttempted] = useState(false);

  const contentRef = useRef<HTMLTextAreaElement>(null);
  const firstPlatformRef = useRef<HTMLInputElement>(null);

  // `useId` rather than hard-coded ids: the label/description wiring stays
  // correct even if two composers ever render on one page.
  const baseId = useId();
  const headingId = `${baseId}-heading`;
  const contentId = `${baseId}-content`;
  const contentHintId = `${baseId}-content-hint`;
  const contentErrorId = `${baseId}-content-error`;
  const platformsHintId = `${baseId}-platforms-hint`;
  const platformsErrorId = `${baseId}-platforms-error`;

  const draft: ComposerDraft = { content, platforms };
  // Derived, not stored: no effect to keep in sync, and no way for the displayed
  // errors to disagree with the current draft.
  const errors: DraftErrors = submitAttempted ? validateDraft(draft) : NO_ERRORS;

  const used = characterLength(content);

  function handleContentChange(event: ChangeEvent<HTMLTextAreaElement>): void {
    setContent(event.target.value);
  }

  function togglePlatform(platform: Platform): void {
    setPlatforms((current) =>
      current.includes(platform)
        ? current.filter((value) => value !== platform)
        : // Rebuilt from the allow-list so the submitted order is the displayed
          // order regardless of which box was ticked first.
          PLATFORM_ALLOW_LIST.filter((value) => value === platform || current.includes(value)),
    );
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    // The page must not navigate: the caller submits this form over fetch.
    event.preventDefault();

    // A disabled button cannot be clicked, but a form can still be submitted by
    // other means, so the guard is here rather than only on the control
    // (Requirement 4.4).
    if (pending) {
      return;
    }

    setSubmitAttempted(true);

    const result = validateDraft(draft);
    if (hasErrors(result)) {
      // Focus the first thing that needs fixing, in DOM order. Leaving focus on
      // the submit button would mean a keyboard user hears nothing and has to go
      // hunting for the message.
      if (result.content !== null) {
        contentRef.current?.focus();
      } else {
        firstPlatformRef.current?.focus();
      }
      return;
    }

    onSubmit(draft);
  }

  return (
    <form
      className="composer"
      aria-labelledby={headingId}
      // Our messages, not the browser's: native constraint validation would
      // block submit with its own tooltip and skip the focus handling above.
      noValidate
      onSubmit={handleSubmit}
    >
      <h2 className="composer__heading" id={headingId}>
        Compose a post
      </h2>

      <div className="composer__field">
        <label className="composer__label" htmlFor={contentId}>
          Post content
        </label>
        <p className="composer__hint" id={contentHintId}>
          Up to {CONTENT_MAX_LENGTH} characters. {used} used.
        </p>
        <textarea
          className="composer__textarea"
          id={contentId}
          name="content"
          rows={6}
          ref={contentRef}
          value={content}
          onChange={handleContentChange}
          aria-invalid={errors.content !== null}
          aria-describedby={
            errors.content === null ? contentHintId : `${contentHintId} ${contentErrorId}`
          }
        />
        {errors.content !== null && (
          <p className="composer__error" id={contentErrorId}>
            {errors.content}
          </p>
        )}
      </div>

      <fieldset
        className="composer__fieldset"
        aria-describedby={
          errors.platforms === null ? platformsHintId : `${platformsHintId} ${platformsErrorId}`
        }
      >
        <legend className="composer__legend">Target platforms</legend>
        <p className="composer__hint" id={platformsHintId}>
          Choose at least one.
        </p>

        <ul className="composer__platforms">
          {PLATFORM_ALLOW_LIST.map((platform, index) => {
            const checkboxId = `${baseId}-platform-${platform}`;
            return (
              <li className="composer__platform" key={platform}>
                <input
                  className="composer__checkbox"
                  type="checkbox"
                  id={checkboxId}
                  name="platforms"
                  value={platform}
                  ref={index === 0 ? firstPlatformRef : null}
                  checked={platforms.includes(platform)}
                  onChange={() => {
                    togglePlatform(platform);
                  }}
                />
                <label className="composer__checkbox-label" htmlFor={checkboxId}>
                  {PLATFORM_LABELS[platform]}
                </label>
              </li>
            );
          })}
        </ul>

        {errors.platforms !== null && (
          <p className="composer__error" id={platformsErrorId}>
            {errors.platforms}
          </p>
        )}
      </fieldset>

      <div className="composer__actions">
        {/* The label changes with the state, so the pending state is carried by
            text and not by a spinner glyph alone: `aria-busy` tells assistive
            technology, "Publishing…" tells everyone else. */}
        <button className="composer__submit" type="submit" disabled={pending} aria-busy={pending}>
          {pending ? 'Publishing…' : 'Publish'}
        </button>
      </div>
    </form>
  );
}
