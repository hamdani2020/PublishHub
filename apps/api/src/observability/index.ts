/**
 * Public surface of the observability module.
 *
 * `bootstrap.ts` is intentionally absent: importing it initializes `dd-trace`, so
 * it is imported by exactly one file, the process entrypoint. Everything exported
 * here is safe to import from a test.
 */

export { INERT_SINK, METRICS, createMetrics } from './metrics.js';
export type { Metrics, MetricsDeps, MetricsSink, SubmissionStatus } from './metrics.js';

export { METRICS_PATH, createMetricsRouter } from './metrics-router.js';
export type { MetricsRouterDeps } from './metrics-router.js';

export { createRequestMetrics } from './request-metrics.js';

export {
  HTTP_HEADERS_FORMAT,
  INERT_TRACING,
  createTracing,
  tracingOptionsFromEnv,
} from './tracing.js';
export type {
  DatadogScope,
  DatadogSpan,
  DatadogSpanContext,
  DatadogTracer,
  Tracing,
  TracerLoader,
  TracingOptions,
} from './tracing.js';
