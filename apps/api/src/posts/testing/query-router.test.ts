/**
 * `GET /api/v1/posts` and `GET /api/v1/posts/:id` (Requirement 2.5).
 *
 * Driven through the real app over HTTP, like the publish tests, with the
 * in-memory Redis fake underneath — so the records these tests read are the ones
 * the publish endpoint and the store actually wrote, encoded the same way.
 *
 * The claims worth asserting: newest-first ordering, that the limit is bounded
 * whatever the client asks for, and that a store which has lost a hash produces a
 * short list rather than a 500.
 */

import type { AddressInfo } from 'node:net';

import { afterEach, describe, expect, it } from 'vitest';

import { createApp } from '../../app.js';
import { loadConfig } from '../../config/index.js';
import { DEPENDENCY_UNAVAILABLE, NOT_FOUND, VALIDATION_FAILED } from '../../http/index.js';
import { createLogger } from '../../logging/index.js';
import { createLogCapture } from '../../logging/testing/log-capture.js';
import type { LogCapture } from '../../logging/testing/log-capture.js';
import { FakeQueueClient } from '../../queue/testing/fake-queue-client.js';
import { FakeRedis } from '../../queue/testing/fake-redis.js';
import { DEFAULT_POST_STORE_KEYS, RedisPostStore } from '../post-store.js';
import type { PostRecord } from '../post-store.js';
import { DEFAULT_POSTS_LIMIT, MAX_POSTS_LIMIT } from '../query-router.js';

interface Harness {
  readonly url: string;
  readonly redis: FakeRedis;
  readonly capture: LogCapture;
  close(): Promise<void>;
}

const harnesses: Harness[] = [];

afterEach(async () => {
  await Promise.all(harnesses.splice(0).map((harness) => harness.close()));
});

async function startApi(): Promise<Harness> {
  const capture = createLogCapture();
  const config = loadConfig({});
  const logger = createLogger(config, { destination: capture.stream });
  const redis = new FakeRedis();

  const app = createApp({ config, logger, redis, queue: new FakeQueueClient() });

  const server = app.listen(0);
  await new Promise<void>((resolve, reject) => {
    server.once('listening', resolve);
    server.once('error', reject);
  });
  const { port } = server.address() as AddressInfo;

  const harness: Harness = {
    url: `http://127.0.0.1:${String(port)}`,
    redis,
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

/** Ids are ULIDs, so a fixed prefix plus an incrementing suffix keeps them ordered. */
function postId(index: number): string {
  return `post_01HZX3QK7M9V4TDR8N2C5EA${String(index).padStart(3, '0')}`;
}

function record(index: number, overrides: Partial<PostRecord> = {}): PostRecord {
  const timestamp = `2026-08-07T10:00:${String(index % 60).padStart(2, '0')}.000Z`;
  return {
    id: postId(index),
    content: `Post number ${String(index)}.`,
    platforms: ['twitter', 'linkedin'],
    status: 'queued',
    job_id: '3f2a9b0c-5d41-4e8b-9c2a-7d6e5f4a3b21',
    created_at: timestamp,
    updated_at: timestamp,
    ...overrides,
  };
}

/**
 * Seed through the real store, so the tests read exactly what production writes.
 * Saved oldest first, which means the index — and the response — is newest first.
 */
async function seed(api: Harness, records: readonly PostRecord[]): Promise<void> {
  const store = new RedisPostStore(api.redis);
  for (const entry of records) {
    await store.save(entry);
  }
}

interface ListBody {
  posts: PostRecord[];
  count: number;
  limit: number;
}

interface ErrorBody {
  error: { code: string; message: string; request_id: string };
}

async function list(api: Harness, query = ''): Promise<Response> {
  return fetch(`${api.url}/api/v1/posts${query}`);
}

describe('GET /api/v1/posts', () => {
  it('returns recent posts newest-first with their current status', async () => {
    const api = await startApi();
    await seed(api, [
      record(1),
      record(2, { status: 'processing' }),
      record(3, { status: 'published', platforms: ['mastodon'] }),
    ]);

    const response = await list(api);
    const body = (await response.json()) as ListBody;

    expect(response.status).toBe(200);
    expect(body.posts.map((post) => post.id)).toEqual([postId(3), postId(2), postId(1)]);
    expect(body.posts.map((post) => post.status)).toEqual(['published', 'processing', 'queued']);
    expect(body.count).toBe(3);
    expect(body.limit).toBe(DEFAULT_POSTS_LIMIT);
  });

  it('decodes a record back to exactly what was stored', async () => {
    const api = await startApi();
    // Content that would not survive a naive encoding: quotes, a newline, an emoji.
    const stored = record(1, { content: 'He said "ship it".\nThen we did 🚀' });
    await seed(api, [stored]);

    const response = await list(api);
    const body = (await response.json()) as ListBody;

    expect(body.posts[0]).toEqual(stored);
  });

  it('returns an empty list rather than a 404 when no posts exist', async () => {
    const api = await startApi();

    const response = await list(api);
    const body = (await response.json()) as ListBody;

    expect(response.status).toBe(200);
    expect(body.posts).toEqual([]);
    expect(body.count).toBe(0);
  });

  it('honours a limit below the number of stored posts', async () => {
    const api = await startApi();
    await seed(api, [record(1), record(2), record(3)]);

    const response = await list(api, '?limit=2');
    const body = (await response.json()) as ListBody;

    expect(body.posts.map((post) => post.id)).toEqual([postId(3), postId(2)]);
    expect(body.limit).toBe(2);
  });

  it('defaults to the documented limit when none is given', async () => {
    const api = await startApi();
    await seed(
      api,
      Array.from({ length: DEFAULT_POSTS_LIMIT + 5 }, (_value, index) => record(index + 1)),
    );

    const body = (await (await list(api)).json()) as ListBody;

    expect(body.count).toBe(DEFAULT_POSTS_LIMIT);
    expect(body.limit).toBe(DEFAULT_POSTS_LIMIT);
  });

  it('clamps a limit above the cap instead of rejecting it', async () => {
    const api = await startApi();
    await seed(api, [record(1), record(2)]);

    const response = await list(api, '?limit=100000');
    const body = (await response.json()) as ListBody;

    expect(response.status).toBe(200);
    expect(body.limit).toBe(MAX_POSTS_LIMIT);
    // Clamping bounds the read: the store is never asked for more than the cap.
    expect(api.redis.calls.find(([command]) => command === 'lrange')).toEqual([
      'lrange',
      DEFAULT_POST_STORE_KEYS.recent,
      0,
      MAX_POSTS_LIMIT - 1,
    ]);
  });

  it.each(['0', '-1', 'twenty', '2.5', '1e2', '', ' 5 ', '5&limit=6'])(
    'rejects limit=%j with 400 VALIDATION_FAILED',
    async (raw) => {
      const api = await startApi();
      await seed(api, [record(1)]);

      const response = await list(api, `?limit=${raw}`);
      const body = (await response.json()) as ErrorBody;

      expect(response.status).toBe(400);
      expect(body.error.code).toBe(VALIDATION_FAILED);
      expect(body.error.request_id).toBe(response.headers.get('x-request-id'));
    },
  );

  it('skips index entries whose record hash has disappeared', async () => {
    const api = await startApi();
    await seed(api, [record(1), record(2), record(3)]);
    // The store unindexes before deleting, and keys can be evicted, so a dangling
    // id is a state the endpoint must survive.
    api.redis.hashes.delete(`publishhub:post:${postId(2)}`);

    const response = await list(api);
    const body = (await response.json()) as ListBody;

    expect(response.status).toBe(200);
    expect(body.posts.map((post) => post.id)).toEqual([postId(3), postId(1)]);
    expect(body.count).toBe(2);
  });

  it('skips a record whose stored fields cannot be decoded', async () => {
    const api = await startApi();
    await seed(api, [record(1), record(2)]);
    await api.redis.hset(`publishhub:post:${postId(2)}`, { platforms: 'not json' });

    const body = (await (await list(api)).json()) as ListBody;

    expect(body.posts.map((post) => post.id)).toEqual([postId(1)]);
  });

  it('returns 503 when the post store is unreachable', async () => {
    const api = await startApi();
    api.redis.lrange = () => Promise.reject(new Error('connect ECONNREFUSED 127.0.0.1:6379'));

    const response = await list(api);
    const raw = await response.text();
    const body = JSON.parse(raw) as ErrorBody;

    expect(response.status).toBe(503);
    expect(body.error.code).toBe(DEPENDENCY_UNAVAILABLE);
    expect(raw).not.toContain('ECONNREFUSED');
  });

  it('is not cacheable, because status changes seconds after submission', async () => {
    const api = await startApi();

    const response = await list(api);

    expect(response.headers.get('cache-control')).toBe('no-store');
  });
});

describe('GET /api/v1/posts/:id', () => {
  it('returns the single record', async () => {
    const api = await startApi();
    const stored = record(2, { status: 'partially_published' });
    await seed(api, [record(1), stored]);

    const response = await fetch(`${api.url}/api/v1/posts/${postId(2)}`);
    const body = (await response.json()) as PostRecord;

    expect(response.status).toBe(200);
    expect(body).toEqual(stored);
    expect(response.headers.get('cache-control')).toBe('no-store');
  });

  it('returns 404 in the standard envelope for an unknown id', async () => {
    const api = await startApi();
    await seed(api, [record(1)]);

    const response = await fetch(`${api.url}/api/v1/posts/post_01HZX3QK7M9V4TDR8N2C5EAZZZ`);
    const body = (await response.json()) as ErrorBody;

    expect(response.status).toBe(404);
    expect(body.error.code).toBe(NOT_FOUND);
    expect(body.error.message).toBe('post not found');
    expect(body.error.request_id).toBe(response.headers.get('x-request-id'));
  });

  it('does not echo the requested id back in the message', async () => {
    const api = await startApi();

    const response = await fetch(`${api.url}/api/v1/posts/%3Cscript%3Ealert(1)%3C%2Fscript%3E`);
    const raw = await response.text();

    expect(response.status).toBe(404);
    expect(raw).not.toContain('script');
  });

  it('reports a record whose hash has been emptied as not found', async () => {
    const api = await startApi();
    await seed(api, [record(1)]);
    api.redis.hashes.delete(`publishhub:post:${postId(1)}`);

    const response = await fetch(`${api.url}/api/v1/posts/${postId(1)}`);
    const body = (await response.json()) as ErrorBody;

    expect(response.status).toBe(404);
    expect(body.error.code).toBe(NOT_FOUND);
  });

  it('returns 503 when the post store is unreachable', async () => {
    const api = await startApi();
    api.redis.hgetall = () => Promise.reject(new Error('READONLY'));

    const response = await fetch(`${api.url}/api/v1/posts/${postId(1)}`);
    const body = (await response.json()) as ErrorBody;

    expect(response.status).toBe(503);
    expect(body.error.code).toBe(DEPENDENCY_UNAVAILABLE);
  });
});
