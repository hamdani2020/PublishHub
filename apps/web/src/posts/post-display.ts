/**
 * Turning a `PostSummary` into the strings the list renders (Requirement 4.5).
 *
 * Kept apart from both the client and the component: these are pure functions
 * over strings, which makes them the cheapest part of the feature to test and the
 * only part with edge cases worth naming (a 5000-character post, an emoji on the
 * truncation boundary, a status the frontend has never heard of).
 */

import { PLATFORM_LABELS, isPlatform } from '../composer';

/**
 * Where a preview is cut, in Unicode code points.
 *
 * The composer allows 5000 characters, so the full body is not something to drop
 * into a list of ten rows. 140 is enough to recognise which post a row is —
 * which is the only job the preview has — while keeping every row a similar
 * height.
 */
export const CONTENT_PREVIEW_MAX = 140;

/** Appended when a preview was cut. A single character, not three dots. */
export const TRUNCATION_SUFFIX = '…';

export interface ContentPreview {
  /** What to render. Never longer than {@link CONTENT_PREVIEW_MAX} plus the suffix. */
  readonly text: string;
  /** True when content was dropped, so the UI can say so rather than imply it. */
  readonly truncated: boolean;
}

/**
 * Cut `content` to a preview.
 *
 * Measured and sliced by code point rather than by UTF-16 code unit, the same way
 * the composer counts: `content.slice(0, 140)` can split a surrogate pair and
 * render a replacement character, which is a visible bug for anyone whose post
 * ends in an emoji.
 *
 * Newlines collapse to spaces. A list row is one line, and a post with a blank
 * line in it would otherwise render as a row with a mysterious gap or, worse,
 * push the rest of the row out of view.
 */
export function previewContent(content: string, max: number = CONTENT_PREVIEW_MAX): ContentPreview {
  const collapsed = content.replace(/\s+/gu, ' ').trim();
  const points = Array.from(collapsed);
  if (points.length <= max) {
    return { text: collapsed, truncated: false };
  }
  // Trailing whitespace before the ellipsis reads as a typo, so it is trimmed
  // after the cut rather than before.
  const cut = points.slice(0, max).join('').trimEnd();
  return { text: `${cut}${TRUNCATION_SUFFIX}`, truncated: true };
}

/**
 * Display names for the statuses in `apps/api/src/posts/post-store.ts`.
 *
 * Worded as states a person can act on rather than as the wire values:
 * "Publishing" tells a reader something is happening now, where `processing`
 * describes what the worker is doing to a queue message.
 */
export const POST_STATUS_LABELS: Readonly<Record<string, string>> = Object.freeze({
  queued: 'Queued',
  processing: 'Publishing',
  published: 'Published',
  partially_published: 'Partly published',
  failed: 'Failed',
});

/**
 * A status the list can show.
 *
 * An unrecognised value is humanised rather than hidden or replaced with
 * "Unknown": the worker owns this vocabulary and may add to it, and a reader is
 * better served by seeing `retry_scheduled` spelled out than by a row that
 * silently claims nothing.
 */
export function statusLabel(status: string): string {
  return POST_STATUS_LABELS[status] ?? humanize(status);
}

/**
 * The modifier suffix for a status, used for the row's colour cue. Grouped rather
 * than one class per status: the list distinguishes "still working", "done",
 * "partly done", and "failed", and an unknown status falls in with "still
 * working" because that is the safest thing to imply about a post whose state we
 * do not recognise.
 */
export function statusTone(status: string): 'pending' | 'success' | 'warning' | 'error' {
  switch (status) {
    case 'published':
      return 'success';
    case 'partially_published':
      return 'warning';
    case 'failed':
      return 'error';
    default:
      return 'pending';
  }
}

/**
 * Platform display names, reusing the composer's labels so a post shows
 * "LinkedIn" in both places. An unrecognised value is humanised for the same
 * reason as an unrecognised status.
 */
export function platformLabel(platform: string): string {
  return isPlatform(platform) ? PLATFORM_LABELS[platform] : humanize(platform);
}

/**
 * Platforms as one readable phrase. A post with no readable platforms is
 * possible only if a record was written outside the API's own validation, so it
 * gets an honest placeholder instead of an empty cell.
 */
export function platformsLabel(platforms: readonly string[]): string {
  if (platforms.length === 0) {
    return 'None recorded';
  }
  return platforms.map(platformLabel).join(', ');
}

/** `partially_published` -> `Partially published`. */
function humanize(value: string): string {
  const spaced = value.replace(/[_-]+/gu, ' ').trim();
  if (spaced === '') {
    return 'Unknown';
  }
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
