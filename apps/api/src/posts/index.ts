/** Public surface of the posts module. */

export { MAX_ULID_TIME_MS, POST_ID_PREFIX, generatePostId } from './post-id.js';

export {
  DEFAULT_POST_STORE_KEYS,
  POST_STATUSES,
  RECENT_POSTS_MAX,
  RedisPostStore,
  decodePostRecord,
  encodePostRecord,
  isPostStatus,
} from './post-store.js';
export type {
  PostRecord,
  PostStatus,
  PostStore,
  PostStoreCommands,
  PostStoreKeys,
  RedisPostStoreOptions,
} from './post-store.js';

export { publishRequestSchema, validatePublishRequest } from './publish-schema.js';
export type { PublishRequest, PublishRequestValidation } from './publish-schema.js';

export { PUBLISH_PATH, createPublishRouter } from './publish-router.js';
export type { PublishAcceptedBody, PublishRouterDeps } from './publish-router.js';

export {
  DEFAULT_POSTS_LIMIT,
  MAX_POSTS_LIMIT,
  POSTS_PATH,
  POST_PATH,
  createQueryRouter,
  parseLimit,
} from './query-router.js';
export type { PostListBody, QueryRouterDeps } from './query-router.js';
