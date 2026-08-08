/** Public surface of the health module. */

export {
  DEFAULT_READINESS_TIMEOUT_MS,
  DEPENDENCY_UNAVAILABLE,
  createHealthRouter,
} from './health.js';
export type {
  CheckResult,
  HealthRouterDeps,
  LivenessBody,
  QueueCheckResult,
  QueueProbe,
  ReadinessBody,
  RedisProbe,
} from './health.js';
