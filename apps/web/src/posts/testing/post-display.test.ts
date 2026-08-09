import { describe, expect, it } from 'vitest';

import {
  CONTENT_PREVIEW_MAX,
  platformsLabel,
  previewContent,
  statusLabel,
  statusTone,
} from '../post-display';

/**
 * The display helpers behind the recent posts list (Requirement 4.5).
 *
 * These are pure string functions, so this is where the edge cases live: the
 * truncation boundary, an emoji that occupies two UTF-16 units, and values the
 * frontend has never seen.
 */

describe('previewContent', () => {
  it('leaves content at or under the limit untouched', () => {
    expect(previewContent('Shipping the platform today.')).toEqual({
      text: 'Shipping the platform today.',
      truncated: false,
    });

    const exact = 'a'.repeat(CONTENT_PREVIEW_MAX);
    expect(previewContent(exact)).toEqual({ text: exact, truncated: false });
  });

  it('cuts at the limit and marks the result truncated', () => {
    const preview = previewContent('a'.repeat(CONTENT_PREVIEW_MAX + 1));

    expect(preview).toEqual({ text: `${'a'.repeat(CONTENT_PREVIEW_MAX)}…`, truncated: true });
  });

  it('counts and cuts by code point so an emoji is never split', () => {
    // Each rocket is one code point and two UTF-16 units: a naive slice at the
    // limit would cut one in half and render a replacement character.
    const content = '🚀'.repeat(CONTENT_PREVIEW_MAX + 10);

    const preview = previewContent(content);

    expect(preview.truncated).toBe(true);
    expect(Array.from(preview.text)).toHaveLength(CONTENT_PREVIEW_MAX + 1);
    expect(preview.text).not.toContain('\uFFFD');
    expect(preview.text.endsWith('🚀…')).toBe(true);
  });

  it('collapses whitespace so a multi-line post stays one row', () => {
    expect(previewContent('  First line.\n\n  Second line.\t').text).toBe('First line. Second line.');
  });

  it('does not leave a space before the ellipsis', () => {
    const content = `${'a'.repeat(CONTENT_PREVIEW_MAX - 1)} tail`;

    expect(previewContent(content).text).toBe(`${'a'.repeat(CONTENT_PREVIEW_MAX - 1)}…`);
  });

  it('respects an explicit limit', () => {
    expect(previewContent('abcdef', 3)).toEqual({ text: 'abc…', truncated: true });
  });
});

describe('statusLabel and statusTone', () => {
  it('labels every status the API can write', () => {
    expect(statusLabel('queued')).toBe('Queued');
    expect(statusLabel('processing')).toBe('Publishing');
    expect(statusLabel('published')).toBe('Published');
    expect(statusLabel('partially_published')).toBe('Partly published');
    expect(statusLabel('failed')).toBe('Failed');
  });

  it('humanizes a status the frontend does not know', () => {
    expect(statusLabel('retry_scheduled')).toBe('Retry scheduled');
  });

  it('groups statuses into the four tones the rows distinguish', () => {
    expect(statusTone('published')).toBe('success');
    expect(statusTone('partially_published')).toBe('warning');
    expect(statusTone('failed')).toBe('error');
    expect(statusTone('queued')).toBe('pending');
    expect(statusTone('processing')).toBe('pending');
    // An unknown state is reported as still in progress: the safest thing to
    // imply about a post whose status we cannot interpret.
    expect(statusTone('retry_scheduled')).toBe('pending');
  });
});

describe('platformsLabel', () => {
  it('uses the composer labels and keeps the submitted order', () => {
    expect(platformsLabel(['twitter', 'linkedin', 'mastodon', 'bluesky'])).toBe(
      'Twitter, LinkedIn, Mastodon, Bluesky',
    );
  });

  it('humanizes a platform outside the allow-list', () => {
    expect(platformsLabel(['threads'])).toBe('Threads');
  });

  it('says so rather than rendering an empty cell', () => {
    expect(platformsLabel([])).toBe('None recorded');
  });
});
