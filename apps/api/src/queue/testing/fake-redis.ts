/**
 * In-memory stand-in for the Redis commands this service uses — the queue
 * client's lists, the post store's hashes, and `PING` for the readiness probe —
 * so tests exercise the real client code without a Redis server.
 *
 * List orientation matches Redis: index 0 is the head (`LPUSH` side) and the
 * last element is the tail (`RPOPLPUSH` side), which is what makes the queue
 * FIFO.
 */

import type { PostStoreCommands } from '../../posts/index.js';
import type { RedisCommands } from '../redis-queue-client.js';

export class FakeRedis implements RedisCommands, PostStoreCommands {
  readonly lists = new Map<string, string[]>();
  readonly hashes = new Map<string, Record<string, string>>();
  /** Every command in order, for asserting sequences such as push-then-remove. */
  readonly calls: Array<[string, ...unknown[]]> = [];
  closed = false;

  private list(key: string): string[] {
    const existing = this.lists.get(key);
    if (existing !== undefined) {
      return existing;
    }
    const created: string[] = [];
    this.lists.set(key, created);
    return created;
  }

  /** Contents head-first, the way `LRANGE key 0 -1` would report them. */
  contents(key: string): string[] {
    return [...this.list(key)];
  }

  async lpush(key: string, value: string): Promise<number> {
    this.calls.push(['lpush', key, value]);
    const list = this.list(key);
    list.unshift(value);
    return list.length;
  }

  async rpoplpush(source: string, destination: string): Promise<string | null> {
    this.calls.push(['rpoplpush', source, destination]);
    const from = this.list(source);
    const value = from.pop();
    if (value === undefined) {
      return null;
    }
    this.list(destination).unshift(value);
    return value;
  }

  async brpoplpush(
    source: string,
    destination: string,
    timeoutSeconds: number,
  ): Promise<string | null> {
    this.calls.push(['brpoplpush', source, destination, timeoutSeconds]);
    const from = this.list(source);
    const value = from.pop();
    if (value === undefined) {
      // A real Redis would block for `timeoutSeconds` and then return null; the
      // fake reports the same outcome immediately.
      return null;
    }
    this.list(destination).unshift(value);
    return value;
  }

  /** `LRANGE key start stop`: head-first, `stop` inclusive, negatives allowed. */
  async lrange(key: string, start: number, stop: number): Promise<string[]> {
    this.calls.push(['lrange', key, start, stop]);
    const list = this.list(key);
    const from = start < 0 ? Math.max(list.length + start, 0) : start;
    const to = stop < 0 ? list.length + stop : Math.min(stop, list.length - 1);
    return from > to ? [] : list.slice(from, to + 1);
  }

  async lrem(key: string, count: number, value: string): Promise<number> {
    this.calls.push(['lrem', key, count, value]);
    const list = this.list(key);
    const limit = count === 0 ? Number.POSITIVE_INFINITY : Math.abs(count);
    let removed = 0;
    for (let index = 0; index < list.length && removed < limit; ) {
      if (list[index] === value) {
        list.splice(index, 1);
        removed += 1;
      } else {
        index += 1;
      }
    }
    return removed;
  }

  async llen(key: string): Promise<number> {
    this.calls.push(['llen', key]);
    return this.list(key).length;
  }

  /** `LTRIM key start stop`, supporting the negative indexes Redis accepts. */
  async ltrim(key: string, start: number, stop: number): Promise<'OK'> {
    this.calls.push(['ltrim', key, start, stop]);
    const list = this.list(key);
    const from = start < 0 ? Math.max(list.length + start, 0) : start;
    const to = stop < 0 ? list.length + stop : Math.min(stop, list.length - 1);
    const kept = from > to ? [] : list.slice(from, to + 1);
    list.length = 0;
    list.push(...kept);
    return 'OK';
  }

  async hset(key: string, values: Record<string, string>): Promise<number> {
    this.calls.push(['hset', key, values]);
    const existing = this.hashes.get(key) ?? {};
    const added = Object.keys(values).filter((field) => !(field in existing)).length;
    this.hashes.set(key, { ...existing, ...values });
    return added;
  }

  async hgetall(key: string): Promise<Record<string, string>> {
    this.calls.push(['hgetall', key]);
    // Redis reports a missing hash as an empty one rather than as null.
    return { ...(this.hashes.get(key) ?? {}) };
  }

  async del(key: string): Promise<number> {
    this.calls.push(['del', key]);
    const removed = (this.hashes.delete(key) ? 1 : 0) + (this.lists.delete(key) ? 1 : 0);
    return removed > 0 ? 1 : 0;
  }

  async ping(): Promise<'PONG'> {
    this.calls.push(['ping']);
    return 'PONG';
  }

  async quit(): Promise<'OK'> {
    this.calls.push(['quit']);
    this.closed = true;
    return 'OK';
  }
}
