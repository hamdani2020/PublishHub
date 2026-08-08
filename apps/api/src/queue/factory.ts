/**
 * Backend selection (Requirements 5.1, 5.4, 5.5).
 *
 * Switching between Redis and SQS is an environment-variable change and nothing
 * else: no business logic branches on the backend. When the selection or its
 * required settings are wrong, this fails at startup with a message that names
 * the offending key, rather than surfacing a connection error later from a
 * request handler.
 *
 * Keys read here match the configuration reference in the design document:
 * `QUEUE_BACKEND`, `REDIS_URL`, `SQS_QUEUE_URL`, `AWS_REGION`, plus the optional
 * `SQS_DLQ_URL` for explicit dead-lettering instead of relying on the queue's
 * redrive policy.
 */

import { Redis } from 'ioredis';

import { AwsSqsPort } from './aws-sqs-port.js';
import { RedisQueueClient } from './redis-queue-client.js';
import type { RedisCommands, RedisQueueClientOptions } from './redis-queue-client.js';
import { SqsQueueClient } from './sqs-queue-client.js';
import type { SqsPort } from './sqs-queue-client.js';
import { QueueConfigError } from './types.js';
import type { DeadLetterEvent, QueueBackend, QueueClient } from './types.js';

export const QUEUE_BACKENDS: readonly QueueBackend[] = ['redis', 'sqs'];
export const DEFAULT_QUEUE_BACKEND: QueueBackend = 'redis';
export const DEFAULT_REDIS_URL = 'redis://publishhub-redis:6379';
export const DEFAULT_AWS_REGION = 'us-east-1';

export type QueueConfig =
  | { readonly backend: 'redis'; readonly redisUrl: string }
  | {
      readonly backend: 'sqs';
      readonly queueUrl: string;
      readonly deadLetterQueueUrl: string | null;
      readonly region: string;
    };

export type Env = Record<string, string | undefined>;

function read(env: Env, key: string): string | undefined {
  const value = env[key];
  if (value === undefined) {
    return undefined;
  }
  const trimmed = value.trim();
  return trimmed === '' ? undefined : trimmed;
}

function requireUrl(key: string, value: string, protocols: readonly string[]): string {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new QueueConfigError(key, `${key} is not a valid URL: ${JSON.stringify(value)}`);
  }
  if (!protocols.includes(parsed.protocol)) {
    throw new QueueConfigError(
      key,
      `${key} must use one of ${protocols.join(', ')} — received ${JSON.stringify(parsed.protocol)}`,
    );
  }
  return value;
}

/**
 * Pure configuration resolution. Throws `QueueConfigError` with the offending
 * key so startup validation and tests can assert on the key rather than on
 * message wording.
 */
export function resolveQueueConfig(env: Env = process.env): QueueConfig {
  const requested = read(env, 'QUEUE_BACKEND')?.toLowerCase() ?? DEFAULT_QUEUE_BACKEND;

  if (!(QUEUE_BACKENDS as readonly string[]).includes(requested)) {
    throw new QueueConfigError(
      'QUEUE_BACKEND',
      `QUEUE_BACKEND must be one of ${QUEUE_BACKENDS.join(', ')} — received ${JSON.stringify(requested)}`,
    );
  }

  if (requested === 'redis') {
    const redisUrl = read(env, 'REDIS_URL') ?? DEFAULT_REDIS_URL;
    return {
      backend: 'redis',
      redisUrl: requireUrl('REDIS_URL', redisUrl, ['redis:', 'rediss:']),
    };
  }

  const queueUrl = read(env, 'SQS_QUEUE_URL');
  if (queueUrl === undefined) {
    throw new QueueConfigError(
      'SQS_QUEUE_URL',
      'SQS_QUEUE_URL is required when QUEUE_BACKEND=sqs',
    );
  }

  const deadLetterQueueUrl = read(env, 'SQS_DLQ_URL');

  return {
    backend: 'sqs',
    queueUrl: requireUrl('SQS_QUEUE_URL', queueUrl, ['https:', 'http:']),
    deadLetterQueueUrl:
      deadLetterQueueUrl === undefined
        ? null
        : requireUrl('SQS_DLQ_URL', deadLetterQueueUrl, ['https:', 'http:']),
    region: read(env, 'AWS_REGION') ?? DEFAULT_AWS_REGION,
  };
}

export interface QueueClientDeps {
  /** Overridable so tests never open a socket. */
  createRedis?: ((redisUrl: string) => RedisCommands) | undefined;
  createSqsPort?: ((options: { region: string }) => SqsPort) | undefined;
  onDeadLetter?: ((event: DeadLetterEvent) => void) | undefined;
  redisKeys?: RedisQueueClientOptions['keys'];
}

function defaultRedis(redisUrl: string): RedisCommands {
  return new Redis(redisUrl, {
    // Connect on first command so construction cannot throw at import time and
    // readiness, not startup, decides when the dependency is required.
    lazyConnect: true,
    // Blocking commands must not be aborted by a retry cap while they wait.
    maxRetriesPerRequest: null,
  });
}

/** Build the client for the configured backend. */
export function createQueueClientFromConfig(
  config: QueueConfig,
  deps: QueueClientDeps = {},
): QueueClient {
  if (config.backend === 'redis') {
    const redis = (deps.createRedis ?? defaultRedis)(config.redisUrl);
    return new RedisQueueClient(redis, {
      keys: deps.redisKeys,
      onDeadLetter: deps.onDeadLetter,
    });
  }

  const createSqsPort = deps.createSqsPort ?? ((options) => new AwsSqsPort(options));
  const port = createSqsPort({ region: config.region });
  return new SqsQueueClient(port, {
    queueUrl: config.queueUrl,
    deadLetterQueueUrl: config.deadLetterQueueUrl ?? undefined,
    onDeadLetter: deps.onDeadLetter,
  });
}

/** Resolve configuration from the environment, then build the client. */
export function createQueueClient(env: Env = process.env, deps: QueueClientDeps = {}): QueueClient {
  return createQueueClientFromConfig(resolveQueueConfig(env), deps);
}
