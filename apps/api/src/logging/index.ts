/** Public surface of the logging module. */

export { createLogger } from './logger.js';
export type { LoggerDeps, TraceContextProvider } from './logger.js';

export {
  CORRELATION_ID_ALT_HEADER,
  CORRELATION_ID_HEADER,
  createRequestLogger,
  requestPath,
  resolveCorrelationId,
} from './request-logger.js';
export type { RequestLogFields } from './request-logger.js';
