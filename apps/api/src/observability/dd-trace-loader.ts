/**
 * The only file in the service that names `dd-trace`.
 *
 * Kept to one function so that "is the APM library in this process" has exactly
 * one answer: it is there if and only if {@link loadDatadogTracer} was called,
 * which only happens when `OBSERVABILITY_ENABLED` is true (Requirement 14.6).
 *
 * The load is synchronous, through `createRequire`, and that is the point. The
 * tracer has to patch `http`, `express`, and `ioredis` before those modules are
 * evaluated, and an ES module's static imports are all evaluated before the
 * importing module's body runs. `await import('dd-trace')` in the entrypoint would
 * therefore run *after* everything it needs to patch. A synchronous require from
 * the first-evaluated module gets there first. `dd-trace` is CommonJS, so this
 * asks nothing unusual of it.
 */

import { createRequire } from 'node:module';

import type { DatadogTracer } from './tracing.js';

const requireCjs = createRequire(import.meta.url);

export function loadDatadogTracer(): DatadogTracer {
  return requireCjs('dd-trace') as DatadogTracer;
}
