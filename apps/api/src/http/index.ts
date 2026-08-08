/** Public surface of the HTTP support module. */

export {
  DEPENDENCY_UNAVAILABLE,
  INTERNAL_ERROR,
  NOT_FOUND,
  PAYLOAD_TOO_LARGE,
  QUEUE_UNAVAILABLE,
  VALIDATION_FAILED,
  errorEnvelope,
  resolveRequestId,
  sendError,
} from './errors.js';
export type { ErrorEnvelope } from './errors.js';

export {
  INTERNAL_ERROR_MESSAGE,
  createErrorHandler,
  createNotFoundHandler,
} from './error-handler.js';

export { JSON_BODY_LIMIT, createCors, createSecurityHeaders, resolveAllowedOrigins } from './security.js';

export {
  DEFAULT_SHUTDOWN_GRACE_MS,
  SHUTDOWN_SIGNALS,
  createShutdown,
  installShutdownHandlers,
} from './shutdown.js';
export type {
  DrainableServer,
  InstallShutdownDeps,
  Shutdown,
  ShutdownDeps,
  ShutdownOutcome,
  ShutdownResource,
  SignalTarget,
} from './shutdown.js';
