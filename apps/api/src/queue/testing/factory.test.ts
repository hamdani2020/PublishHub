/**
 * Factory and configuration tests (Requirements 5.1, 5.4, 5.5).
 *
 * Two behaviors matter here: switching backends is an environment change and
 * nothing else, and a bad or missing setting fails immediately with the name of
 * the key that is wrong.
 */

import { describe, expect, it } from 'vitest';

import {
  DEFAULT_AWS_REGION,
  DEFAULT_REDIS_URL,
  createQueueClient,
  resolveQueueConfig,
} from '../factory.js';
import { RedisQueueClient } from '../redis-queue-client.js';
import { SqsQueueClient } from '../sqs-queue-client.js';
import { FakeRedis } from './fake-redis.js';
import { FakeSqsPort } from './fake-sqs-port.js';
import { QueueConfigError } from '../types.js';

const QUEUE_URL = 'https://sqs.us-east-1.amazonaws.com/123456789012/publishhub-jobs';
const DLQ_URL = 'https://sqs.us-east-1.amazonaws.com/123456789012/publishhub-jobs-dlq';

const fakes = {
  createRedis: () => new FakeRedis(),
  createSqsPort: () => new FakeSqsPort(),
};

function expectConfigError(env: Record<string, string | undefined>, key: string): QueueConfigError {
  let thrown: unknown;
  try {
    resolveQueueConfig(env);
  } catch (error) {
    thrown = error;
  }

  expect(thrown, `expected resolveQueueConfig to reject ${key}`).toBeInstanceOf(QueueConfigError);
  const error = thrown as QueueConfigError;
  expect(error.key).toBe(key);
  // The message names the offending key, so the failure is actionable.
  expect(error.message).toContain(key);
  return error;
}

describe('resolveQueueConfig', () => {
  it('defaults to the Redis backend for local development', () => {
    expect(resolveQueueConfig({})).toEqual({
      backend: 'redis',
      redisUrl: DEFAULT_REDIS_URL,
    });
  });

  it('ignores case and surrounding whitespace in QUEUE_BACKEND', () => {
    expect(resolveQueueConfig({ QUEUE_BACKEND: '  SQS ', SQS_QUEUE_URL: QUEUE_URL })).toEqual({
      backend: 'sqs',
      queueUrl: QUEUE_URL,
      deadLetterQueueUrl: null,
      region: DEFAULT_AWS_REGION,
    });
  });

  it('treats an empty value as unset rather than as an error', () => {
    expect(resolveQueueConfig({ QUEUE_BACKEND: '', REDIS_URL: '   ' })).toEqual({
      backend: 'redis',
      redisUrl: DEFAULT_REDIS_URL,
    });
  });

  it('accepts an explicit Redis url, including TLS', () => {
    expect(resolveQueueConfig({ REDIS_URL: 'rediss://cache.example:6380' })).toEqual({
      backend: 'redis',
      redisUrl: 'rediss://cache.example:6380',
    });
  });

  it('reads the optional SQS dead-letter queue url and region', () => {
    expect(
      resolveQueueConfig({
        QUEUE_BACKEND: 'sqs',
        SQS_QUEUE_URL: QUEUE_URL,
        SQS_DLQ_URL: DLQ_URL,
        AWS_REGION: 'eu-west-1',
      }),
    ).toEqual({
      backend: 'sqs',
      queueUrl: QUEUE_URL,
      deadLetterQueueUrl: DLQ_URL,
      region: 'eu-west-1',
    });
  });

  it('fails fast naming QUEUE_BACKEND when the backend is unknown', () => {
    const error = expectConfigError({ QUEUE_BACKEND: 'kafka' }, 'QUEUE_BACKEND');
    expect(error.message).toContain('redis, sqs');
  });

  it('fails fast naming SQS_QUEUE_URL when it is missing for the sqs backend', () => {
    expectConfigError({ QUEUE_BACKEND: 'sqs' }, 'SQS_QUEUE_URL');
    expectConfigError({ QUEUE_BACKEND: 'sqs', SQS_QUEUE_URL: '   ' }, 'SQS_QUEUE_URL');
  });

  it('fails fast naming the key when a url is malformed', () => {
    expectConfigError({ REDIS_URL: 'not-a-url' }, 'REDIS_URL');
    expectConfigError({ REDIS_URL: 'http://localhost:6379' }, 'REDIS_URL');
    expectConfigError({ QUEUE_BACKEND: 'sqs', SQS_QUEUE_URL: 'sqs-queue' }, 'SQS_QUEUE_URL');
    expectConfigError(
      { QUEUE_BACKEND: 'sqs', SQS_QUEUE_URL: QUEUE_URL, SQS_DLQ_URL: 'nope' },
      'SQS_DLQ_URL',
    );
  });
});

describe('createQueueClient', () => {
  it('builds the Redis client when the backend is redis', () => {
    const client = createQueueClient({ QUEUE_BACKEND: 'redis' }, fakes);
    expect(client).toBeInstanceOf(RedisQueueClient);
  });

  it('builds the SQS client when the backend is sqs', () => {
    const client = createQueueClient({ QUEUE_BACKEND: 'sqs', SQS_QUEUE_URL: QUEUE_URL }, fakes);
    expect(client).toBeInstanceOf(SqsQueueClient);
  });

  it('exposes one interface, so callers never branch on the backend', async () => {
    const job = {
      schema_version: 1 as const,
      job_id: '3f2a9b0c-5d41-4e8b-9c2a-7d6e5f4a3b21',
      post_id: 'post_01HZX3QK7M9V4TDR8N2C5EAB6F',
      content: 'same code path either way',
      platforms: ['twitter' as const],
      attempt: 1,
      enqueued_at: '2026-08-07T10:00:00.000Z',
      trace_context: {},
    };

    for (const env of [
      { QUEUE_BACKEND: 'redis' },
      { QUEUE_BACKEND: 'sqs', SQS_QUEUE_URL: QUEUE_URL },
    ]) {
      const client = createQueueClient(env, fakes);

      await client.enqueue(job);
      const received = await client.receive(0);

      expect(received?.job, `backend ${String(env.QUEUE_BACKEND)}`).toEqual(job);
      await client.ack(received!);
      await client.close();
    }
  });

  it('propagates the dead-letter listener to the selected backend', async () => {
    const events: string[] = [];
    const client = createQueueClient(
      { QUEUE_BACKEND: 'redis' },
      { ...fakes, onDeadLetter: (event) => events.push(`${event.backend}:${event.reason}`) },
    );

    const job = {
      schema_version: 1 as const,
      job_id: '3f2a9b0c-5d41-4e8b-9c2a-7d6e5f4a3b21',
      post_id: 'post_01HZX3QK7M9V4TDR8N2C5EAB6F',
      content: 'doomed',
      platforms: ['twitter' as const],
      attempt: 3,
      enqueued_at: '2026-08-07T10:00:00.000Z',
      trace_context: {},
    };
    await client.enqueue(job);
    const received = await client.receive(0);
    await client.deadLetter(received!, 'max_attempts_exhausted');

    expect(events).toEqual(['redis:max_attempts_exhausted']);
  });

  it('does not read the environment when configuration is passed explicitly', () => {
    // Guards against an accidental `process.env` default leaking into a test run.
    expect(() => resolveQueueConfig({ QUEUE_BACKEND: 'sqs' })).toThrow(QueueConfigError);
  });
});
