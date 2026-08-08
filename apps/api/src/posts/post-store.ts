/**
 * Post record store: a Redis hash per post plus a recent-posts index.
 *
 * Post state lives in Redis in every environment — only the *queue* swaps
 * between Redis and SQS. That is a deliberate tradeoff recorded in the design:
 * one stateful dependency instead of adding DynamoDB or RDS, at the cost of
 * durability a real product would need.
 *
 * | Key                          | Type | Holds                                  |
 * |------------------------------|------|----------------------------------------|
 * | `publishhub:post:<id>`       | hash | One post record, field per attribute.   |
 * | `publishhub:posts:recent`    | list | Post ids, newest first, capped.         |
 *
 * A hash rather than a serialized JSON string because the worker updates only
 * `status`, `updated_at`, and the per-platform results when a job finishes: with
 * a hash that is one `HSET` of the changed fields, where a JSON blob would need a
 * read-modify-write and would lose a concurrent update.
 *
 * The recent index is a capped list, not a sorted set. Post ids are ULIDs, so
 * insertion order already is time order, and `LPUSH` + `LTRIM` bounds the
 * memory of a store that has no eviction policy of its own. `GET /api/v1/posts`
 * reads it with `LRANGE` and resolves each id with `HGETALL`, which is why the
 * index cap is also the ceiling on what that endpoint can return.
 *
 * Reads are forgiving by design. An index entry whose hash is gone is skipped
 * rather than surfaced, and a hash that cannot be decoded reads as absent — see
 * {@link decodePostRecord}. A store with no durability guarantees will
 * occasionally hold a dangling id, and that is not a reason to fail a query.
 *
 * Write ordering is chosen so that a crash between two commands leaves the store
 * readable rather than lying:
 *
 * - Saving writes the hash *before* the index entry, so the index never points at
 *   a record that does not exist.
 * - Removing drops the index entry *before* the hash, so the same invariant holds
 *   from the other direction; the worst case is an orphaned hash that nothing
 *   lists.
 */

import { isPlatform } from '../queue/index.js';
import type { Platform } from '../queue/index.js';

/**
 * Post lifecycle. The API only ever writes `queued`; the rest are written by the
 * worker as it processes the job (spec task 4.2) and are listed here so the
 * shared vocabulary lives in one place.
 */
export const POST_STATUSES = [
  'queued',
  'processing',
  'published',
  'partially_published',
  'failed',
] as const;

export type PostStatus = (typeof POST_STATUSES)[number];

export function isPostStatus(value: unknown): value is PostStatus {
  return typeof value === 'string' && (POST_STATUSES as readonly string[]).includes(value);
}

export interface PostRecord {
  /** `post_` + 26-char Crockford base32 ULID. Also the client-visible `id`. */
  readonly id: string;
  /** The body exactly as submitted: never truncated or normalized. */
  readonly content: string;
  /** Targets in submission order, deduplicated, all from the allow-list. */
  readonly platforms: readonly Platform[];
  readonly status: PostStatus;
  /** The envelope's `job_id`, so a post record and its queue message correlate. */
  readonly job_id: string;
  /** RFC 3339 UTC with millisecond precision. */
  readonly created_at: string;
  readonly updated_at: string;
}

export const DEFAULT_POST_STORE_KEYS = {
  /** Full record key is this prefix plus the post id. */
  postPrefix: 'publishhub:post:',
  recent: 'publishhub:posts:recent',
} as const;

export type PostStoreKeys = { -readonly [K in keyof typeof DEFAULT_POST_STORE_KEYS]: string };

/**
 * How many ids the recent index keeps. Redis holds post state with no eviction
 * of its own, so the index is bounded here; the cap is also the ceiling on what
 * `GET /api/v1/posts` can return.
 */
export const RECENT_POSTS_MAX = 100;

/**
 * The narrow slice of Redis the store uses. `ioredis` satisfies it structurally,
 * and the tests pass an in-memory fake, so no test needs a Redis server.
 */
export interface PostStoreCommands {
  hset(key: string, values: Record<string, string>): Promise<number>;
  /** Redis reports a missing hash as an empty one, never as null. */
  hgetall(key: string): Promise<Record<string, string>>;
  del(key: string): Promise<number>;
  lpush(key: string, value: string): Promise<number>;
  lrange(key: string, start: number, stop: number): Promise<string[]>;
  lrem(key: string, count: number, value: string): Promise<number>;
  ltrim(key: string, start: number, stop: number): Promise<unknown>;
}

export interface PostStore {
  /** Persist the record and index it as the newest post. */
  save(record: PostRecord): Promise<void>;
  /**
   * Delete the record and unindex it. Used to compensate a post that was
   * persisted and then could not be enqueued, so a failed submission leaves
   * nothing behind.
   */
  remove(postId: string): Promise<void>;
  /** One record, or null when no readable record exists under that id. */
  get(postId: string): Promise<PostRecord | null>;
  /** Newest first, at most `limit` records. */
  listRecent(limit: number): Promise<PostRecord[]>;
}

/** Hash fields for one record. Every value is a string: Redis stores no types. */
export function encodePostRecord(record: PostRecord): Record<string, string> {
  return {
    id: record.id,
    content: record.content,
    // JSON rather than a comma-joined string: the platform list is a list, and
    // encoding it as one keeps the Python worker's decode trivial and unambiguous.
    platforms: JSON.stringify(record.platforms),
    status: record.status,
    job_id: record.job_id,
    created_at: record.created_at,
    updated_at: record.updated_at,
  };
}

/**
 * The inverse of {@link encodePostRecord}.
 *
 * Returns `null` rather than throwing for anything it cannot turn into a
 * `PostRecord`: a missing hash (Redis hands back `{}`), a field the worker has
 * not written, unparseable `platforms`, or a `status` outside the shared
 * vocabulary. The read paths treat `null` as "no readable record" — a corrupt or
 * half-written hash must not turn a query into a 500, and a partially decoded
 * record would be worse than none, because the client cannot tell which fields
 * it can trust.
 */
export function decodePostRecord(fields: Record<string, string>): PostRecord | null {
  const { id, content, platforms, status, job_id, created_at, updated_at } = fields;
  if (
    id === undefined ||
    content === undefined ||
    platforms === undefined ||
    job_id === undefined ||
    created_at === undefined ||
    updated_at === undefined ||
    !isPostStatus(status)
  ) {
    return null;
  }

  const decodedPlatforms = decodePlatforms(platforms);
  if (decodedPlatforms === null) {
    return null;
  }

  return { id, content, platforms: decodedPlatforms, status, job_id, created_at, updated_at };
}

function decodePlatforms(encoded: string): Platform[] | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(encoded);
  } catch {
    return null;
  }
  if (!Array.isArray(parsed) || parsed.length === 0 || !parsed.every(isPlatform)) {
    return null;
  }
  return parsed;
}

export interface RedisPostStoreOptions {
  keys?: Partial<PostStoreKeys> | undefined;
  /** Defaults to {@link RECENT_POSTS_MAX}. */
  recentLimit?: number | undefined;
}

export class RedisPostStore implements PostStore {
  private readonly redis: PostStoreCommands;
  private readonly keys: PostStoreKeys;
  private readonly recentLimit: number;

  constructor(redis: PostStoreCommands, options: RedisPostStoreOptions = {}) {
    this.redis = redis;
    this.keys = { ...DEFAULT_POST_STORE_KEYS, ...(options.keys ?? {}) };
    this.recentLimit = options.recentLimit ?? RECENT_POSTS_MAX;
  }

  /** `publishhub:post:<id>`. Exposed so a caller can name the key in a log line. */
  recordKey(postId: string): string {
    return `${this.keys.postPrefix}${postId}`;
  }

  get recentKey(): string {
    return this.keys.recent;
  }

  async save(record: PostRecord): Promise<void> {
    await this.redis.hset(this.recordKey(record.id), encodePostRecord(record));
    await this.redis.lpush(this.keys.recent, record.id);
    // Trim on every write rather than on a schedule: it is O(1) amortized for a
    // single-element overflow and it means the index cannot grow unbounded even
    // if nothing else ever runs.
    await this.redis.ltrim(this.keys.recent, 0, this.recentLimit - 1);
  }

  async remove(postId: string): Promise<void> {
    // Count 1: ids are unique, so there is exactly one entry to drop, and a
    // bounded `LREM` beats scanning the whole list.
    await this.redis.lrem(this.keys.recent, 1, postId);
    await this.redis.del(this.recordKey(postId));
  }

  async get(postId: string): Promise<PostRecord | null> {
    return decodePostRecord(await this.redis.hgetall(this.recordKey(postId)));
  }

  /**
   * The newest `limit` ids from the index, resolved to records.
   *
   * Entries whose hash has disappeared are skipped, so the result can be shorter
   * than `limit`. That is not a hypothetical: `remove` unindexes before deleting,
   * a key can be evicted, and Redis here is not durable storage. The index is the
   * ordering, not the truth.
   *
   * The hash reads are issued together rather than one after another: `limit` is
   * bounded by the index cap, and a serial loop would make the endpoint's latency
   * the sum of up to a hundred round trips. `Promise.all` preserves order, so
   * newest-first survives.
   */
  async listRecent(limit: number): Promise<PostRecord[]> {
    const capped = Math.min(Math.trunc(limit), this.recentLimit);
    if (!Number.isFinite(capped) || capped <= 0) {
      return [];
    }

    // `LRANGE` stop is inclusive.
    const ids = await this.redis.lrange(this.keys.recent, 0, capped - 1);
    const records = await Promise.all(ids.map((id) => this.get(id)));
    return records.filter((record): record is PostRecord => record !== null);
  }
}
