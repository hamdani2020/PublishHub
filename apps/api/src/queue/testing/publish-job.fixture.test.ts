/**
 * Contract test: the TypeScript envelope implementation against the shared
 * fixture both languages read (Requirement 5.6).
 *
 * These assertions are the drift alarm. If the fixture and this implementation
 * disagree, one of them changed without the other, and `docs/message-schema.md`
 * says they move together.
 */

import { describe, expect, it } from 'vitest';

import {
  CONTENT_MAX_LENGTH,
  CONTENT_MIN_LENGTH,
  ENQUEUED_AT_PATTERN,
  JOB_ID_PATTERN,
  PLATFORM_ALLOW_LIST,
  POST_ID_PATTERN,
  parsePublishJob,
  serializePublishJob,
  validatePublishJob,
} from '../publish-job.js';
import { loadPublishJobFixture } from './fixture.js';
import { SCHEMA_VERSION } from '../types.js';

const fixture = loadPublishJobFixture();

describe('fixture agreement', () => {
  it('describes the schema version this build implements', () => {
    expect(fixture.schema_version).toBe(SCHEMA_VERSION);
  });

  it('matches the constraints the implementation enforces', () => {
    expect(fixture.constraints.content_min_length).toBe(CONTENT_MIN_LENGTH);
    expect(fixture.constraints.content_max_length).toBe(CONTENT_MAX_LENGTH);
    expect(fixture.constraints.platform_allow_list).toEqual([...PLATFORM_ALLOW_LIST]);
    expect(fixture.constraints.patterns.job_id).toBe(JOB_ID_PATTERN.source);
    expect(fixture.constraints.patterns.post_id).toBe(POST_ID_PATTERN.source);
    expect(fixture.constraints.patterns.enqueued_at).toBe(ENQUEUED_AT_PATTERN.source);
  });
});

describe('canonical message', () => {
  it('round-trips byte-for-byte through parse and serialize', () => {
    const raw = JSON.stringify(fixture.canonical);
    const parsed = parsePublishJob(raw);

    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;

    expect(serializePublishJob(parsed.job)).toBe(raw);
  });

  it('serializes exactly the required field set, nothing more or less', () => {
    const parsed = parsePublishJob(JSON.stringify(fixture.canonical));
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;

    const keys = Object.keys(JSON.parse(serializePublishJob(parsed.job)) as object);
    expect(keys).toEqual(fixture.required_fields);
  });
});

describe('valid variants', () => {
  for (const variant of fixture.variants) {
    it(`accepts ${variant.name}: ${variant.description}`, () => {
      const result = parsePublishJob(JSON.stringify(variant.message));
      expect(result.ok, `expected ${variant.name} to be accepted`).toBe(true);
    });
  }

  it('drops unknown top-level fields instead of rejecting the message', () => {
    const variant = fixture.variants.find((entry) => entry.name === 'unknown_field_forward_compat');
    expect(variant, 'fixture must cover forward compatibility').toBeDefined();

    const result = parsePublishJob(JSON.stringify(variant?.message));
    expect(result.ok).toBe(true);
    if (!result.ok) return;

    expect(Object.keys(result.job)).toEqual(fixture.required_fields);
    expect(result.job).not.toHaveProperty('scheduled_for');
  });
});

describe('invalid messages', () => {
  for (const entry of fixture.invalid) {
    it(`dead-letters ${entry.name} with reason ${entry.reason}`, () => {
      const raw = entry.raw ?? JSON.stringify(entry.message);
      const result = parsePublishJob(raw);

      expect(result.ok).toBe(false);
      if (result.ok) return;

      expect(result.reason).toBe(entry.reason);
      expect(fixture.constraints.dead_letter_reasons).toContain(result.reason);
      expect(result.detail).not.toBe('');
    });
  }
});

describe('content length bounds', () => {
  const base = fixture.canonical;

  it('accepts content at the maximum length', () => {
    const atLimit = { ...base, content: 'a'.repeat(fixture.constraints.content_max_length) };
    expect(validatePublishJob(atLimit).ok).toBe(true);
  });

  it('rejects content one character over the maximum', () => {
    const overLimit = {
      ...base,
      content: 'a'.repeat(fixture.constraints.content_max_length + 1),
    };
    const result = validatePublishJob(overLimit);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.reason).toBe('schema_validation_failed');
  });

  it('counts characters rather than UTF-16 code units at the boundary', () => {
    // Every emoji is one character but two UTF-16 code units, so a naive
    // `content.length` check would reject this valid message.
    const emojiCount = fixture.constraints.content_max_length;
    const atLimit = { ...base, content: '🚀'.repeat(emojiCount) };

    expect(validatePublishJob(atLimit).ok).toBe(true);
    expect(validatePublishJob({ ...base, content: '🚀'.repeat(emojiCount + 1) }).ok).toBe(false);
  });
});
