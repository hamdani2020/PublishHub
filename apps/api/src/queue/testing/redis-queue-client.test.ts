/**
 * Redis backend unit tests against an in-memory fake (Requirement 5.2).
 *
 * The point of these is the reliable-queue behavior: a claimed message lives in
 * the processing list until it is acked or dead-lettered, so nothing is lost if
 * the consumer dies mid-job.
 */

import { beforeEach, describe, expect, it } from 'vitest';

import { createPublishJob, serializePublishJob } from '../publish-job.js';
import { DEFAULT_REDIS_QUEUE_KEYS, RedisQueueClient } from '../redis-queue-client.js';
import { FakeRedis } from './fake-redis.js';
import type { DeadLetterEvent, PublishJob } from '../types.js';

const KEYS = DEFAULT_REDIS_QUEUE_KEYS;

function job(overrides: Partial<Parameters<typeof createPublishJob>[0]> = {}): PublishJob {
  return createPublishJob({
    post_id: 'post_01HZX3QK7M9V4TDR8N2C5EAB6F',
    content: 'hello',
    platforms: ['twitter'],
    ...overrides,
  });
}

describe('RedisQueueClient', () => {
  let redis: FakeRedis;
  let deadLettered: DeadLetterEvent[];
  let client: RedisQueueClient;

  beforeEach(() => {
    redis = new FakeRedis();
    deadLettered = [];
    client = new RedisQueueClient(redis, { onDeadLetter: (event) => deadLettered.push(event) });
  });

  it('enqueues the serialized envelope onto the jobs list', async () => {
    const enqueued = job();
    await client.enqueue(enqueued);

    expect(redis.contents(KEYS.jobs)).toEqual([serializePublishJob(enqueued)]);
    expect(await client.depth()).toBe(1);
  });

  it('serves messages first in, first out', async () => {
    const first = job({ content: 'first' });
    const second = job({ content: 'second' });
    await client.enqueue(first);
    await client.enqueue(second);

    expect((await client.receive(5))?.job?.content).toBe('first');
    expect((await client.receive(5))?.job?.content).toBe('second');
  });

  it('moves a claimed message to the processing list rather than dropping it', async () => {
    await client.enqueue(job());

    const received = await client.receive(20);

    expect(received).not.toBeNull();
    expect(redis.contents(KEYS.jobs)).toEqual([]);
    expect(redis.contents(KEYS.processing)).toEqual([received?.raw]);
    expect(received?.handle).toEqual({ backend: 'redis', payload: received?.raw });
  });

  it('blocks with BRPOPLPUSH for a positive wait and polls with RPOPLPUSH for zero', async () => {
    await client.enqueue(job());
    await client.receive(20);
    expect(redis.calls).toContainEqual(['brpoplpush', KEYS.jobs, KEYS.processing, 20]);

    await client.enqueue(job());
    await client.receive(0);
    expect(redis.calls).toContainEqual(['rpoplpush', KEYS.jobs, KEYS.processing]);
  });

  it('truncates a fractional wait and treats a negative wait as no wait', async () => {
    await client.enqueue(job());
    await client.receive(2.9);
    expect(redis.calls).toContainEqual(['brpoplpush', KEYS.jobs, KEYS.processing, 2]);

    await client.enqueue(job());
    await client.receive(-5);
    expect(redis.calls).toContainEqual(['rpoplpush', KEYS.jobs, KEYS.processing]);
  });

  it('returns null when the queue is empty', async () => {
    expect(await client.receive(1)).toBeNull();
  });

  it('removes the message from processing on ack', async () => {
    await client.enqueue(job());
    const received = await client.receive(5);

    await client.ack(received!);

    expect(redis.contents(KEYS.processing)).toEqual([]);
    expect(redis.contents(KEYS.deadLetter)).toEqual([]);
  });

  it('pushes to the dead-letter list before clearing processing', async () => {
    await client.enqueue(job());
    const received = await client.receive(5);

    await client.deadLetter(received!, 'max_attempts_exhausted');

    expect(redis.contents(KEYS.deadLetter)).toEqual([received?.raw]);
    expect(redis.contents(KEYS.processing)).toEqual([]);

    const commands = redis.calls.map(([command, key]) => `${command} ${String(key)}`);
    expect(commands.indexOf(`lpush ${KEYS.deadLetter}`)).toBeLessThan(
      commands.lastIndexOf(`lrem ${KEYS.processing}`),
    );
  });

  it('dead-letters the payload unchanged so it stays replayable', async () => {
    const enqueued = job();
    await client.enqueue(enqueued);
    const received = await client.receive(5);

    await client.deadLetter(received!, 'schema_validation_failed');

    expect(redis.contents(KEYS.deadLetter)).toEqual([serializePublishJob(enqueued)]);
  });

  it('reports the dead-letter reason and job identity for logging', async () => {
    const enqueued = job();
    await client.enqueue(enqueued);
    const received = await client.receive(5);

    await client.deadLetter(received!, 'max_attempts_exhausted');

    expect(deadLettered).toEqual([
      {
        backend: 'redis',
        reason: 'max_attempts_exhausted',
        jobId: enqueued.job_id,
        postId: enqueued.post_id,
        attempt: 1,
        viaRedrivePolicy: false,
      },
    ]);
  });

  it('surfaces an unparseable payload instead of throwing, and can dead-letter it', async () => {
    await redis.lpush(KEYS.jobs, '{"schema_version": 1, "job_id": "3f2a9b0c-5d41-4e8b');

    const received = await client.receive(5);

    expect(received?.job).toBeNull();
    expect(received?.invalidReason).toBe('unparseable_payload');
    expect(received?.invalidDetail).toBeTruthy();

    await client.deadLetter(received!, received!.invalidReason!);

    expect(redis.contents(KEYS.deadLetter)).toEqual([received?.raw]);
    expect(deadLettered[0]?.jobId).toBeNull();
  });

  it('reports an unknown schema version as such, not as a validation failure', async () => {
    await redis.lpush(
      KEYS.jobs,
      JSON.stringify({ ...job(), schema_version: 2 }),
    );

    const received = await client.receive(5);

    expect(received?.invalidReason).toBe('unknown_schema_version');
  });

  it('honors overridden key names so tests and tenants can be isolated', async () => {
    const scoped = new RedisQueueClient(redis, { keys: { jobs: 'scoped:jobs' } });
    await scoped.enqueue(job());

    expect(redis.contents('scoped:jobs')).toHaveLength(1);
    expect(redis.contents(KEYS.jobs)).toEqual([]);
  });

  it('rejects a handle from the other backend rather than silently no-oping', async () => {
    await expect(
      client.ack({
        raw: '{}',
        handle: { backend: 'sqs', messageId: 'm', receiptHandle: 'r' },
        job: null,
        invalidReason: null,
        invalidDetail: null,
      }),
    ).rejects.toThrow(/not portable across backends/);
  });

  it('reports queue, processing, and dead-letter depths', async () => {
    await client.enqueue(job());
    await client.enqueue(job());
    const received = await client.receive(5);
    await client.deadLetter(received!, 'max_attempts_exhausted');

    expect(await client.depth()).toBe(1);
    expect(await client.processingDepth()).toBe(0);
    expect(await client.deadLetterDepth()).toBe(1);
  });

  it('closes the underlying connection', async () => {
    await client.close();
    expect(redis.closed).toBe(true);
  });
});
