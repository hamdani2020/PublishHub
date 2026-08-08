/**
 * Structured logging (Requirement 14.3).
 *
 * One JSON object per line, on stdout, with `service`, `env`, and — when a
 * tracer is active — the trace identifiers, so a log line and the span that
 * produced it correlate without any manual stitching. Nothing here imports
 * `dd-trace`: the tracer is loaded conditionally by the observability wiring,
 * which passes a `traceContext` function in. With tracing off, the extra fields
 * simply never appear.
 *
 * `level` is a string, not pino's numeric default. A number is cheaper to write
 * and useless to read in a log explorer.
 */

import { hostname } from 'node:os';

import { pino } from 'pino';
import type { DestinationStream, Logger, LoggerOptions } from 'pino';

import type { ApiConfig, LogLevel } from '../config/index.js';

/**
 * Returns the fields that tie a log line to the active span, typically
 * `{ dd: { trace_id, span_id } }` or flat `trace_id` / `span_id` keys. Returns
 * undefined when no span is active.
 */
export type TraceContextProvider = () => Record<string, unknown> | undefined;

export interface LoggerDeps {
  /** Overridable so tests can read what was written instead of stdout. */
  destination?: DestinationStream;
  /** Overrides the level derived from `NODE_ENV`. */
  level?: LogLevel | 'warn' | 'error' | 'silent';
  traceContext?: TraceContextProvider;
}

export function createLogger(config: ApiConfig, deps: LoggerDeps = {}): Logger {
  const { traceContext } = deps;

  const options: LoggerOptions = {
    level: deps.level ?? config.logLevel,
    base: {
      service: config.observability.service,
      env: config.observability.env,
      ...(config.observability.version === null ? {} : { version: config.observability.version }),
      pid: process.pid,
      hostname: hostname(),
    },
    formatters: {
      level: (label) => ({ level: label }),
    },
    timestamp: pino.stdTimeFunctions.isoTime,
    ...(traceContext === undefined ? {} : { mixin: () => traceContext() ?? {} }),
  };

  return deps.destination === undefined ? pino(options) : pino(options, deps.destination);
}
