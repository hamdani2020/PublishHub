/**
 * Post id generation.
 *
 * The format is not cosmetic: `POST_ID_PATTERN` in the queue module is what the
 * envelope validator enforces, so an id this generator produced must satisfy the
 * same regex, or every publish request would fail while building its envelope.
 */

import { describe, expect, it } from 'vitest';

import { POST_ID_PATTERN } from '../../queue/index.js';
import { MAX_ULID_TIME_MS, POST_ID_PREFIX, generatePostId } from '../post-id.js';

describe('generatePostId', () => {
  it('produces ids matching the documented message-schema pattern', () => {
    for (let index = 0; index < 200; index += 1) {
      expect(generatePostId()).toMatch(POST_ID_PATTERN);
    }
  });

  it('produces ids of the prefix plus 26 characters', () => {
    const id = generatePostId();

    expect(id.startsWith(POST_ID_PREFIX)).toBe(true);
    expect(id.slice(POST_ID_PREFIX.length)).toHaveLength(26);
  });

  it('does not repeat within the same millisecond', () => {
    const fixed = new Date('2026-08-07T10:00:00.000Z');
    const ids = new Set(Array.from({ length: 1000 }, () => generatePostId(fixed)));

    expect(ids.size).toBe(1000);
  });

  it('sorts lexicographically by creation time', () => {
    // The property the recent-posts index leans on: ULID time prefix means
    // insertion order is already time order.
    const earlier = generatePostId(new Date('2026-08-07T10:00:00.000Z'));
    const later = generatePostId(new Date('2026-08-07T10:00:00.001Z'));
    const muchLater = generatePostId(new Date('2027-01-01T00:00:00.000Z'));

    expect([muchLater, later, earlier].sort()).toEqual([earlier, later, muchLater]);
  });

  it('rejects a timestamp a 48-bit ULID cannot express', () => {
    expect(() => generatePostId(new Date(MAX_ULID_TIME_MS + 1))).toThrow(RangeError);
  });
});
