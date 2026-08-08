/**
 * Public surface of the queue abstraction. Callers import from here and stay
 * unaware of which backend is active.
 */

export type {
  DeadLetterEvent,
  DeadLetterReason,
  JobHandle,
  Platform,
  PublishJob,
  QueueBackend,
  QueueClient,
  ReceivedJob,
} from './types.js';
export { QueueConfigError, SCHEMA_VERSION } from './types.js';

export {
  CONTENT_MAX_LENGTH,
  CONTENT_MIN_LENGTH,
  ENQUEUED_AT_PATTERN,
  JOB_ID_PATTERN,
  PLATFORM_ALLOW_LIST,
  POST_ID_PATTERN,
  characterLength,
  createPublishJob,
  describeJob,
  formatEnqueuedAt,
  isPlatform,
  parsePublishJob,
  serializePublishJob,
  validatePublishJob,
} from './publish-job.js';
export type { CreatePublishJobInput, ParseResult } from './publish-job.js';

export { DEFAULT_REDIS_QUEUE_KEYS, RedisQueueClient } from './redis-queue-client.js';
export type {
  RedisCommands,
  RedisQueueClientOptions,
  RedisQueueKeys,
} from './redis-queue-client.js';

export { SQS_MAX_WAIT_SECONDS, SqsQueueClient } from './sqs-queue-client.js';
export type { SqsMessage, SqsPort, SqsQueueClientOptions } from './sqs-queue-client.js';

export { AwsSqsPort } from './aws-sqs-port.js';
export type { AwsSqsPortOptions } from './aws-sqs-port.js';

export {
  DEFAULT_AWS_REGION,
  DEFAULT_QUEUE_BACKEND,
  DEFAULT_REDIS_URL,
  QUEUE_BACKENDS,
  createQueueClient,
  createQueueClientFromConfig,
  resolveQueueConfig,
} from './factory.js';
export type { Env, QueueClientDeps, QueueConfig } from './factory.js';
