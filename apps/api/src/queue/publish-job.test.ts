/**
 * Unit tests for the envelope codec: the specific cases and edges that the
 * shared fixture does not cover, mainly the producer side (`createPublishJob`).
 */

import { describe, expect, it } from 'vitest';

import {
  JOB_ID_PATTERN,
  characterLength,
  createPublishJob,
  describeJob,
  formatEnqueuedAt,
  isPlatform,
  parsePublishJob,
  serializePublishJob,
} from './publish-job.js';
import { SCHEMA_VERSION } from './types.js';

const POST_ID = 'post_01HZX3QK7M9V4TDR8N2C5EAB6F';

describe('createPublishJob', () => {
  it('fills the defaults a first attempt needs', () => {
    const job = createPublishJob({ post_id: POST_ID, content: 'hello', platforms: ['twitter'] });

    expect(job.schema_version).toBe(SCHEMA_VERSION);
    expect(job.job_id).toMatch(JOB_ID_PATTERN);
    expect(job.attempt).toBe(1);
    expect(job.trace_context).toEqual({});
    expect(job.enqueued_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
  });

  it('keeps job_id stable and refreshes enqueued_at on a retry', () => {
    const first = createPublishJob({ post_id: POST_ID, content: 'hello', platforms: ['twitter'] });
    const retry = createPublishJob({
      post_id: first.post_id,
      content: first.content,
      platforms: first.platforms,
      job_id: first.job_id,
      attempt: first.attempt + 1,
      enqueued_at: formatEnqueuedAt(new Date('2026-08-07T10:00:07.500Z')),
    });

    expect(retry.job_id).toBe(first.job_id);
    expect(retry.attempt).toBe(2);
    expect(retry.enqueued_at).toBe('2026-08-07T10:00:07.500Z');
  });

  it('refuses to build a message that would be dead-lettered on arrival', () => {
    expect(() =>
      createPublishJob({ post_id: POST_ID, content: '   ', platforms: ['twitter'] }),
    ).toThrow(/content must not be blank/);

    expect(() => createPublishJob({ post_id: POST_ID, content: 'hi', platforms: [] })).toThrow(
      /platforms must be a non-empty array/,
    );

    expect(() =>
      createPublishJob({ post_id: 'post_nope', content: 'hi', platforms: ['twitter'] }),
    ).toThrow(/post_id/);
  });

  it('produces a message that parses back to itself', () => {
    const job = createPublishJob({
      post_id: POST_ID,
      content: 'Déployé 🚀',
      platforms: ['twitter', 'bluesky'],
      trace_context: { 'x-datadog-trace-id': '1' },
    });

    const parsed = parsePublishJob(serializePublishJob(job));
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    expect(parsed.job).toEqual(job);
  });
});

describe('parsePublishJob', () => {
  it('reports unparseable payloads without throwing', () => {
    const result = parsePublishJob('not json at all');

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.reason).toBe('unparseable_payload');
  });

  it('rejects a null trace_context but accepts an empty object', () => {
    const base = {
      schema_version: 1,
      job_id: '3f2a9b0c-5d41-4e8b-9c2a-7d6e5f4a3b21',
      post_id: POST_ID,
      content: 'hi',
      platforms: ['twitter'],
      attempt: 1,
      enqueued_at: '2026-08-07T10:00:00.000Z',
    };

    expect(parsePublishJob(JSON.stringify({ ...base, trace_context: {} })).ok).toBe(true);
    expect(parsePublishJob(JSON.stringify({ ...base, trace_context: null })).ok).toBe(false);
    expect(parsePublishJob(JSON.stringify({ ...base, trace_context: { a: 1 } })).ok).toBe(false);
  });
});

describe('helpers', () => {
  it('counts characters, not UTF-16 code units', () => {
    expect(characterLength('🚀')).toBe(1);
    expect('🚀'.length).toBe(2);
  });

  it('recognizes only allow-listed platforms', () => {
    expect(isPlatform('twitter')).toBe(true);
    expect(isPlatform('Twitter')).toBe(false);
    expect(isPlatform('myspace')).toBe(false);
  });

  it('truncates sub-millisecond precision in enqueued_at', () => {
    expect(formatEnqueuedAt(new Date('2026-08-07T10:00:00.123Z'))).toBe('2026-08-07T10:00:00.123Z');
  });

  it('describes a payload that never parsed without throwing', () => {
    expect(describeJob(null)).toEqual({ jobId: null, postId: null, attempt: null });
  });
});
