/**
 * Redis backend — the free, offline-friendly local queue (Requirement 5.2).
 *
 * | Operation    | Redis commands                                        |
 * |--------------|-------------------------------------------------------|
 * | `enqueue`    | `LPUSH publishhub:jobs`                               |
 * | `receive`    | `BRPOPLPUSH jobs -> jobs:processing` (reliable queue)  |
 * | `ack`        | `LREM jobs:processing 1 <payload>`                    |
 * | `deadLetter` | `LPUSH jobs:dlq` then `LREM jobs:processing`           |
 * | `depth`      | `LLEN publishhub:jobs`                                |
 *
 * `BRPOPLPUSH` rather than `BRPOP`: a worker killed mid-job leaves the message
 * in the processing list instead of dropping it. A reaper on worker startup
 * returns entries older than the visibility window to the main queue — that
 * reaper lives on the consumer side (spec task 2.3), because the API only ever
 * produces.
 */

import { describeJob, parsePublishJob, serializePublishJob } from './publish-job.js';
import type {
  DeadLetterEvent,
  JobHandle,
  PublishJob,
  QueueClient,
  ReceivedJob,
} from './types.js';

export const DEFAULT_REDIS_QUEUE_KEYS = {
  /** Also the `listName` the KEDA redis scaler watches. */
  jobs: 'publishhub:jobs',
  processing: 'publishhub:jobs:processing',
  deadLetter: 'publishhub:jobs:dlq',
} as const;

export type RedisQueueKeys = { -readonly [K in keyof typeof DEFAULT_REDIS_QUEUE_KEYS]: string };

/**
 * The narrow slice of Redis this client uses. `ioredis` satisfies it
 * structurally, and the unit tests pass an in-memory fake, so no test needs a
 * running Redis.
 */
export interface RedisCommands {
  lpush(key: string, value: string): Promise<number>;
  brpoplpush(source: string, destination: string, timeoutSeconds: number): Promise<string | null>;
  rpoplpush(source: string, destination: string): Promise<string | null>;
  lrem(key: string, count: number, value: string): Promise<number>;
  llen(key: string): Promise<number>;
  quit(): Promise<unknown>;
}

export interface RedisQueueClientOptions {
  keys?: Partial<RedisQueueKeys> | undefined;
  onDeadLetter?: ((event: DeadLetterEvent) => void) | undefined;
}

export class RedisQueueClient implements QueueClient {
  private readonly redis: RedisCommands;
  private readonly keys: RedisQueueKeys;
  private readonly onDeadLetter: (event: DeadLetterEvent) => void;

  constructor(redis: RedisCommands, options: RedisQueueClientOptions = {}) {
    this.redis = redis;
    this.keys = { ...DEFAULT_REDIS_QUEUE_KEYS, ...(options.keys ?? {}) };
    this.onDeadLetter = options.onDeadLetter ?? (() => {});
  }

  async enqueue(job: PublishJob): Promise<void> {
    await this.redis.lpush(this.keys.jobs, serializePublishJob(job));
  }

  async receive(waitSeconds: number): Promise<ReceivedJob | null> {
    const timeout = Math.max(0, Math.trunc(waitSeconds));

    // `BRPOPLPUSH ... 0` blocks forever in Redis, whereas SQS treats a zero wait
    // as "return immediately". The non-blocking variant keeps both backends
    // behaving the same way for the same argument.
    const payload =
      timeout === 0
        ? await this.redis.rpoplpush(this.keys.jobs, this.keys.processing)
        : await this.redis.brpoplpush(this.keys.jobs, this.keys.processing, timeout);

    if (payload === null) {
      return null;
    }

    const handle: JobHandle = { backend: 'redis', payload };
    const parsed = parsePublishJob(payload);

    return parsed.ok
      ? { raw: payload, handle, job: parsed.job, invalidReason: null, invalidDetail: null }
      : {
          raw: payload,
          handle,
          job: null,
          invalidReason: parsed.reason,
          invalidDetail: parsed.detail,
        };
  }

  async ack(job: ReceivedJob): Promise<void> {
    const payload = this.processingMember(job);
    await this.redis.lrem(this.keys.processing, 1, payload);
  }

  async deadLetter(job: ReceivedJob, reason: string): Promise<void> {
    const payload = this.processingMember(job);

    // Push before removing from `processing`: a crash between the two leaves a
    // duplicate in the dead-letter list, which is recoverable, where the
    // opposite order would lose the message outright.
    //
    // The dead-letter entry is the raw payload, byte-for-byte, so a message can
    // be replayed into either backend. Redis lists carry no per-entry metadata,
    // so the reason travels through `onDeadLetter` for the caller to log
    // (the SQS backend additionally attaches it as a message attribute).
    await this.redis.lpush(this.keys.deadLetter, payload);
    await this.redis.lrem(this.keys.processing, 1, payload);

    const { jobId, postId, attempt } = describeJob(job.job);
    this.onDeadLetter({
      backend: 'redis',
      reason,
      jobId,
      postId,
      attempt,
      viaRedrivePolicy: false,
    });
  }

  async depth(): Promise<number> {
    return this.redis.llen(this.keys.jobs);
  }

  /** Pending entries claimed but not yet acked. Used by the consumer's reaper. */
  async processingDepth(): Promise<number> {
    return this.redis.llen(this.keys.processing);
  }

  async deadLetterDepth(): Promise<number> {
    return this.redis.llen(this.keys.deadLetter);
  }

  async close(): Promise<void> {
    await this.redis.quit();
  }

  private processingMember(job: ReceivedJob): string {
    if (job.handle.backend !== 'redis') {
      throw new TypeError(
        `RedisQueueClient received a "${job.handle.backend}" handle; handles are not portable across backends`,
      );
    }
    return job.handle.payload;
  }
}
