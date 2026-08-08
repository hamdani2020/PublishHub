/**
 * `POST /api/v1/publish` (Requirements 2.1, 2.2).
 *
 * Driven through the real app over HTTP, like the health tests, because the
 * behavior under test is HTTP-shaped: status codes, the error envelope, and — the
 * claim that matters most — whether a rejected or failed request left anything
 * behind in Redis or on the queue.
 *
 * The fakes are the in-memory Redis and queue client from `queue/testing`, so
 * every assertion about "what was persisted" reads the same data structures the
 * real clients would have written to.
 */

import type { AddressInfo } from 'node:net';

import { afterEach, describe, expect, it } from 'vitest';

import { createApp } from '../../app.js';
import { loadConfig } from '../../config/index.js';
import {
  DEPENDENCY_UNAVAILABLE,
  QUEUE_UNAVAILABLE,
  VALIDATION_FAILED,
} from '../../http/index.js';
import { createLogger } from '../../logging/index.js';
import { createLogCapture } from '../../logging/testing/log-capture.js';
import type { LogCapture } from '../../logging/testing/log-capture.js';
import { CONTENT_MAX_LENGTH, POST_ID_PATTERN, SCHEMA_VERSION } from '../../queue/index.js';
import { FakeQueueClient } from '../../queue/testing/fake-queue-client.js';
import { FakeRedis } from '../../queue/testing/fake-redis.js';
import { DEFAULT_POST_STORE_KEYS } from '../post-store.js';

interface Harness {
  readonly url: string;
  readonly redis: FakeRedis;
  readonly queue: FakeQueueClient;
  readonly capture: LogCapture;
  close(): Promise<void>;
}

const harnesses: Harness[] = [];

afterEach(async () => {
  await Promise.all(harnesses.splice(0).map((harness) => harness.close()));
});

async function startApi(
  options: { now?: () => Date; generatePostId?: () => string } = {},
): Promise<Harness> {
  const capture = createLogCapture();
  const config = loadConfig({});
  const logger = createLogger(config, { destination: capture.stream });
  const redis = new FakeRedis();
  const queue = new FakeQueueClient();

  const app = createApp({
    config,
    logger,
    redis,
    queue,
    now: options.now,
    generatePostId: options.generatePostId,
  });

  const server = app.listen(0);
  await new Promise<void>((resolve, reject) => {
    server.once('listening', resolve);
    server.once('error', reject);
  });
  const { port } = server.address() as AddressInfo;

  const harness: Harness = {
    url: `http://127.0.0.1:${String(port)}`,
    redis,
    queue,
    capture,
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.close((error) => {
          if (error) {
            reject(error);
          } else {
            resolve();
          }
        });
      }),
  };
  harnesses.push(harness);
  return harness;
}

async function publish(api: Harness, body: unknown): Promise<Response> {
  return fetch(`${api.url}/api/v1/publish`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

interface ErrorBody {
  error: { code: string; message: string; request_id: string };
}

function recentIds(api: Harness): string[] {
  return api.redis.contents(DEFAULT_POST_STORE_KEYS.recent);
}

describe('POST /api/v1/publish — valid input', () => {
  it('returns 202 with the generated id and queued status', async () => {
    const api = await startApi();

    const response = await publish(api, {
      content: 'Shipping PublishHub: kind + ArgoCD + KEDA, all from one Makefile.',
      platforms: ['twitter', 'linkedin'],
    });
    const body = (await response.json()) as { id: string; status: string };

    expect(response.status).toBe(202);
    expect(body.status).toBe('queued');
    expect(body.id).toMatch(POST_ID_PATTERN);
  });

  it('persists the post record and indexes it as the newest post', async () => {
    const api = await startApi({ now: () => new Date('2026-08-07T10:00:00.000Z') });

    const response = await publish(api, {
      content: 'Shipping PublishHub.',
      platforms: ['twitter', 'linkedin'],
    });
    const { id } = (await response.json()) as { id: string };

    const stored = api.redis.hashes.get(`publishhub:post:${id}`);
    expect(stored).toMatchObject({
      id,
      content: 'Shipping PublishHub.',
      platforms: '["twitter","linkedin"]',
      status: 'queued',
      created_at: '2026-08-07T10:00:00.000Z',
      updated_at: '2026-08-07T10:00:00.000Z',
    });
    expect(recentIds(api)).toEqual([id]);
  });

  it('enqueues one envelope that matches the persisted record', async () => {
    const api = await startApi({ now: () => new Date('2026-08-07T10:00:00.000Z') });

    const response = await publish(api, {
      content: 'Shipping PublishHub.',
      platforms: ['twitter', 'linkedin'],
    });
    const { id } = (await response.json()) as { id: string };

    expect(api.queue.enqueued).toHaveLength(1);
    const [job] = api.queue.enqueued;
    expect(job).toMatchObject({
      schema_version: SCHEMA_VERSION,
      post_id: id,
      content: 'Shipping PublishHub.',
      platforms: ['twitter', 'linkedin'],
      attempt: 1,
      enqueued_at: '2026-08-07T10:00:00.000Z',
      // Empty rather than absent: tracing is off, so the worker starts a root span.
      trace_context: {},
    });
    // The record and its message are joined by job_id, which is how a log line
    // about one leads to the other.
    expect(api.redis.hashes.get(`publishhub:post:${id}`)?.job_id).toBe(job!.job_id);
  });

  it('accepts content at exactly the maximum length', async () => {
    const api = await startApi();

    const response = await publish(api, {
      content: 'x'.repeat(CONTENT_MAX_LENGTH),
      platforms: ['twitter'],
    });

    expect(response.status).toBe(202);
    expect(api.queue.enqueued).toHaveLength(1);
  });

  it('strips unknown body fields instead of rejecting them', async () => {
    const api = await startApi();

    const response = await publish(api, {
      content: 'Shipping PublishHub.',
      platforms: ['twitter'],
      // A client must not be able to set its own status or id.
      status: 'published',
      id: 'post_ATTACKERSUPPLIEDVALUE00',
      nonsense: { deeply: ['nested'] },
    });
    const { id } = (await response.json()) as { id: string };

    expect(response.status).toBe(202);
    expect(id).not.toBe('post_ATTACKERSUPPLIEDVALUE00');
    expect(api.redis.hashes.get(`publishhub:post:${id}`)).toMatchObject({ status: 'queued' });
    expect(api.redis.hashes.get(`publishhub:post:${id}`)).not.toHaveProperty('nonsense');
    expect(api.queue.enqueued[0]).not.toHaveProperty('nonsense');
  });

  it('deduplicates repeated platforms while preserving submission order', async () => {
    // Duplicates are not one of Requirement 2.2's 400 cases, but the envelope
    // forbids them, so the request is normalized rather than failed.
    const api = await startApi();

    const response = await publish(api, {
      content: 'Shipping PublishHub.',
      platforms: ['linkedin', 'twitter', 'linkedin'],
    });

    expect(response.status).toBe(202);
    expect(api.queue.enqueued[0]?.platforms).toEqual(['linkedin', 'twitter']);
  });

  it('stores content exactly as submitted, without trimming it', async () => {
    const api = await startApi();

    const response = await publish(api, {
      content: '  leading and trailing space matters to the author  ',
      platforms: ['twitter'],
    });
    const { id } = (await response.json()) as { id: string };

    expect(api.redis.hashes.get(`publishhub:post:${id}`)?.content).toBe(
      '  leading and trailing space matters to the author  ',
    );
  });
});

describe('POST /api/v1/publish — validation failures', () => {
  /**
   * Every case asserts the same three things: the 400, the code, and that nothing
   * was written or enqueued. The last one is the actual requirement (2.2) — a 400
   * that still queued a job would be the interesting bug.
   */
  const cases: Array<{ name: string; body: unknown; message: string }> = [
    {
      name: 'content missing',
      body: { platforms: ['twitter'] },
      message: 'content is required',
    },
    {
      name: 'content empty',
      body: { content: '', platforms: ['twitter'] },
      message: 'content must not be empty',
    },
    {
      name: 'content blank after trimming',
      body: { content: '   \n\t ', platforms: ['twitter'] },
      message: 'content must not be empty',
    },
    {
      name: 'content over the maximum length',
      body: { content: 'x'.repeat(CONTENT_MAX_LENGTH + 1), platforms: ['twitter'] },
      message: `content must be at most ${String(CONTENT_MAX_LENGTH)} characters`,
    },
    {
      name: 'content not a string',
      body: { content: 42, platforms: ['twitter'] },
      message: 'content must be a string',
    },
    {
      name: 'platforms missing',
      body: { content: 'Shipping PublishHub.' },
      message: 'platforms is required',
    },
    {
      name: 'platforms empty',
      body: { content: 'Shipping PublishHub.', platforms: [] },
      message: 'platforms must contain at least one target',
    },
    {
      name: 'platforms not an array',
      body: { content: 'Shipping PublishHub.', platforms: 'twitter' },
      message: 'platforms must be an array',
    },
    {
      name: 'platforms containing an unsupported target',
      body: { content: 'Shipping PublishHub.', platforms: ['twitter', 'myspace'] },
      message: 'platforms must contain only supported targets: twitter, linkedin, mastodon, bluesky',
    },
    {
      name: 'platforms in the wrong case',
      body: { content: 'Shipping PublishHub.', platforms: ['Twitter'] },
      message: 'platforms must contain only supported targets: twitter, linkedin, mastodon, bluesky',
    },
  ];

  for (const testCase of cases) {
    it(`rejects ${testCase.name} without enqueueing`, async () => {
      const api = await startApi();

      const response = await publish(api, testCase.body);
      const body = (await response.json()) as ErrorBody;

      expect(response.status).toBe(400);
      expect(body.error.code).toBe(VALIDATION_FAILED);
      expect(body.error.message).toBe(testCase.message);
      expect(body.error.request_id).toBe(response.headers.get('x-request-id'));
      expect(api.queue.enqueued).toEqual([]);
      expect(api.redis.hashes.size).toBe(0);
      expect(recentIds(api)).toEqual([]);
    });
  }

  it('rejects a body that is not valid JSON', async () => {
    const api = await startApi();

    const response = await fetch(`${api.url}/api/v1/publish`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{"content": "Shipping PublishHub.", "platforms": ["twitter"',
    });
    const body = (await response.json()) as ErrorBody;

    expect(response.status).toBe(400);
    expect(body.error.code).toBe(VALIDATION_FAILED);
    expect(api.queue.enqueued).toEqual([]);
  });

  it('rejects a body that is not an object', async () => {
    const api = await startApi();

    const response = await publish(api, ['twitter']);
    const body = (await response.json()) as ErrorBody;

    expect(response.status).toBe(400);
    expect(body.error.code).toBe(VALIDATION_FAILED);
    expect(api.queue.enqueued).toEqual([]);
  });
});

describe('POST /api/v1/publish — queue unavailable', () => {
  it('returns 503 QUEUE_UNAVAILABLE and leaves no post record behind', async () => {
    const api = await startApi();
    api.queue.enqueueError = new Error(
      'connect ECONNREFUSED 127.0.0.1:6379 (redis://user:s3cret@127.0.0.1:6379)',
    );

    const response = await publish(api, {
      content: 'Shipping PublishHub.',
      platforms: ['twitter'],
    });
    const raw = await response.text();
    const body = JSON.parse(raw) as ErrorBody;

    expect(response.status).toBe(503);
    expect(body.error.code).toBe(QUEUE_UNAVAILABLE);
    // The design's error-handling table: no partial post record left behind.
    expect(api.redis.hashes.size).toBe(0);
    expect(recentIds(api)).toEqual([]);
    // And the client learns the queue is unavailable, not the connection string.
    expect(raw).not.toContain('s3cret');
    expect(raw).not.toContain('ECONNREFUSED');
  });

  it('logs the enqueue failure with the correlation id', async () => {
    const api = await startApi();
    api.queue.enqueueError = new Error('QueueDoesNotExist: the specified queue does not exist');

    const response = await publish(api, {
      content: 'Shipping PublishHub.',
      platforms: ['twitter'],
    });

    const lines = await api.capture.waitFor(2);
    const failure = lines.find((line) => line.msg === 'failed to enqueue publish job');
    expect(failure).toMatchObject({ level: 'error' });
    expect((failure?.err as { message?: string }).message).toContain('QueueDoesNotExist');
    expect(failure?.correlation_id).toBe(response.headers.get('x-request-id'));
  });

  it('returns 503 when the post store is unreachable, without enqueueing', async () => {
    const api = await startApi();
    api.redis.hset = () => Promise.reject(new Error('READONLY You cannot write against a replica'));

    const response = await publish(api, {
      content: 'Shipping PublishHub.',
      platforms: ['twitter'],
    });
    const body = (await response.json()) as ErrorBody;

    expect(response.status).toBe(503);
    expect(body.error.code).toBe(DEPENDENCY_UNAVAILABLE);
    expect(api.queue.enqueued).toEqual([]);
  });

  it('still answers 503 when the rollback itself fails', async () => {
    const api = await startApi();
    api.queue.enqueueError = new Error('queue unreachable');
    api.redis.del = () => Promise.reject(new Error('redis went away mid-rollback'));

    const response = await publish(api, {
      content: 'Shipping PublishHub.',
      platforms: ['twitter'],
    });
    const body = (await response.json()) as ErrorBody;

    expect(response.status).toBe(503);
    expect(body.error.code).toBe(QUEUE_UNAVAILABLE);

    const lines = await api.capture.waitFor(3);
    expect(
      lines.some((line) => line.msg === 'failed to roll back post record after enqueue failure'),
    ).toBe(true);
  });
});
