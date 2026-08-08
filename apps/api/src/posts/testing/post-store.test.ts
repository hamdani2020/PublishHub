/**
 * Post record store.
 *
 * Two behaviors are worth asserting directly rather than through the endpoint:
 * the recent index stays capped, and a removal leaves nothing behind — the second
 * is what makes the publish endpoint's rollback honest.
 */

import { describe, expect, it } from 'vitest';

import { FakeRedis } from '../../queue/testing/fake-redis.js';
import {
  DEFAULT_POST_STORE_KEYS,
  RECENT_POSTS_MAX,
  RedisPostStore,
  decodePostRecord,
  encodePostRecord,
} from '../post-store.js';
import type { PostRecord } from '../post-store.js';

function record(id: string): PostRecord {
  return {
    id,
    content: 'Shipping PublishHub.',
    platforms: ['twitter', 'linkedin'],
    status: 'queued',
    job_id: '3f2a9b0c-5d41-4e8b-9c2a-7d6e5f4a3b21',
    created_at: '2026-08-07T10:00:00.000Z',
    updated_at: '2026-08-07T10:00:00.000Z',
  };
}

describe('RedisPostStore.save', () => {
  it('writes the record hash and indexes the id as newest', async () => {
    const redis = new FakeRedis();
    const store = new RedisPostStore(redis);

    await store.save(record('post_01HZX3QK7M9V4TDR8N2C5EAB6F'));
    await store.save(record('post_01HZX3QK7M9V4TDR8N2C5EAB6G'));

    expect(redis.hashes.get('publishhub:post:post_01HZX3QK7M9V4TDR8N2C5EAB6F')).toEqual(
      encodePostRecord(record('post_01HZX3QK7M9V4TDR8N2C5EAB6F')),
    );
    // Newest first, which is the order `GET /api/v1/posts` needs.
    expect(redis.contents(DEFAULT_POST_STORE_KEYS.recent)).toEqual([
      'post_01HZX3QK7M9V4TDR8N2C5EAB6G',
      'post_01HZX3QK7M9V4TDR8N2C5EAB6F',
    ]);
  });

  it('writes the hash before indexing it, so the index never dangles', async () => {
    const redis = new FakeRedis();
    const store = new RedisPostStore(redis);

    await store.save(record('post_01HZX3QK7M9V4TDR8N2C5EAB6F'));

    expect(redis.calls.map(([command]) => command)).toEqual(['hset', 'lpush', 'ltrim']);
  });

  it('caps the recent index at the configured limit', async () => {
    const redis = new FakeRedis();
    const store = new RedisPostStore(redis, { recentLimit: 3 });

    for (const suffix of ['A', 'B', 'C', 'D', 'E']) {
      await store.save(record(`post_01HZX3QK7M9V4TDR8N2C5EAB6${suffix}`));
    }

    expect(redis.contents(DEFAULT_POST_STORE_KEYS.recent)).toEqual([
      'post_01HZX3QK7M9V4TDR8N2C5EAB6E',
      'post_01HZX3QK7M9V4TDR8N2C5EAB6D',
      'post_01HZX3QK7M9V4TDR8N2C5EAB6C',
    ]);
  });

  it('encodes platforms as JSON so the worker decodes a list, not a string', () => {
    const fields = encodePostRecord(record('post_01HZX3QK7M9V4TDR8N2C5EAB6F'));

    expect(JSON.parse(fields.platforms!)).toEqual(['twitter', 'linkedin']);
    expect(fields.status).toBe('queued');
  });
});

describe('RedisPostStore.remove', () => {
  it('deletes the record and unindexes it', async () => {
    const redis = new FakeRedis();
    const store = new RedisPostStore(redis);
    await store.save(record('post_01HZX3QK7M9V4TDR8N2C5EAB6F'));
    await store.save(record('post_01HZX3QK7M9V4TDR8N2C5EAB6G'));

    await store.remove('post_01HZX3QK7M9V4TDR8N2C5EAB6G');

    expect(redis.hashes.has('publishhub:post:post_01HZX3QK7M9V4TDR8N2C5EAB6G')).toBe(false);
    expect(redis.contents(DEFAULT_POST_STORE_KEYS.recent)).toEqual([
      'post_01HZX3QK7M9V4TDR8N2C5EAB6F',
    ]);
  });
});

describe('decodePostRecord', () => {
  it('round-trips a record through the encoder', () => {
    const original = record('post_01HZX3QK7M9V4TDR8N2C5EAB6F');

    expect(decodePostRecord(encodePostRecord(original))).toEqual(original);
  });

  it('reads a missing hash as absent rather than throwing', () => {
    // Redis reports a missing hash as an empty one, so this is the shape a read
    // of a deleted or evicted key actually returns.
    expect(decodePostRecord({})).toBeNull();
  });

  const base = (): Record<string, string> =>
    encodePostRecord(record('post_01HZX3QK7M9V4TDR8N2C5EAB6F'));

  it.each([
    ['a half-written hash', (): Record<string, string> => ({ id: 'post_x', status: 'queued' })],
    [
      'a missing field',
      (): Record<string, string> => {
        const fields = base();
        delete fields.updated_at;
        return fields;
      },
    ],
    ['unparseable platforms', (): Record<string, string> => ({ ...base(), platforms: '{' })],
    [
      'platforms that is not an array',
      (): Record<string, string> => ({ ...base(), platforms: '"twitter"' }),
    ],
    ['an empty platforms list', (): Record<string, string> => ({ ...base(), platforms: '[]' })],
    [
      'an unsupported platform',
      (): Record<string, string> => ({ ...base(), platforms: '["myspace"]' }),
    ],
    [
      'a status outside the vocabulary',
      (): Record<string, string> => ({ ...base(), status: 'somehow_published' }),
    ],
  ])('reads %s as absent', (_name, fields) => {
    expect(decodePostRecord(fields())).toBeNull();
  });
});

describe('RedisPostStore.get', () => {
  it('returns the stored record', async () => {
    const redis = new FakeRedis();
    const store = new RedisPostStore(redis);
    const stored = record('post_01HZX3QK7M9V4TDR8N2C5EAB6F');
    await store.save(stored);

    expect(await store.get(stored.id)).toEqual(stored);
  });

  it('returns null for an unknown id', async () => {
    const store = new RedisPostStore(new FakeRedis());

    expect(await store.get('post_01HZX3QK7M9V4TDR8N2C5EAB6F')).toBeNull();
  });
});

describe('RedisPostStore.listRecent', () => {
  const ids = ['A', 'B', 'C'].map((suffix) => `post_01HZX3QK7M9V4TDR8N2C5EAB6${suffix}`);

  async function seeded(): Promise<{ redis: FakeRedis; store: RedisPostStore }> {
    const redis = new FakeRedis();
    const store = new RedisPostStore(redis);
    for (const id of ids) {
      await store.save(record(id));
    }
    return { redis, store };
  }

  it('returns records newest first', async () => {
    const { store } = await seeded();

    expect((await store.listRecent(10)).map((entry) => entry.id)).toEqual([...ids].reverse());
  });

  it('returns at most the requested number', async () => {
    const { store } = await seeded();

    expect((await store.listRecent(2)).map((entry) => entry.id)).toEqual([ids[2], ids[1]]);
  });

  it('never reads beyond the index cap, however large the request', async () => {
    const { redis, store } = await seeded();

    await store.listRecent(Number.MAX_SAFE_INTEGER);

    expect(redis.calls.filter(([command]) => command === 'lrange')).toEqual([
      ['lrange', DEFAULT_POST_STORE_KEYS.recent, 0, RECENT_POSTS_MAX - 1],
    ]);
  });

  it('returns nothing for a non-positive limit without touching Redis', async () => {
    const { redis, store } = await seeded();
    const before = redis.calls.length;

    expect(await store.listRecent(0)).toEqual([]);
    expect(redis.calls.length).toBe(before);
  });

  it('skips ids whose record hash is gone instead of failing the read', async () => {
    const { redis, store } = await seeded();
    redis.hashes.delete(`publishhub:post:${ids[1]!}`);

    expect((await store.listRecent(10)).map((entry) => entry.id)).toEqual([ids[2], ids[0]]);
  });

  it('returns an empty list when nothing has been indexed', async () => {
    const store = new RedisPostStore(new FakeRedis());

    expect(await store.listRecent(10)).toEqual([]);
  });
});
