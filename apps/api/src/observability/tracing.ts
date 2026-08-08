/**
 * Datadog tracing, behind a seam (Requirements 14.2, 14.3, 14.6).
 *
 * Nothing in this file imports `dd-trace`. The tracer arrives as a loader
 * function, so the decision "should this process trace at all" is made once, at
 * startup, and the library is only pulled into the process when the answer is
 * yes. That is what makes `OBSERVABILITY_ENABLED=false` genuinely inert rather
 * than merely quiet: an APM library that is loaded has already monkey-patched
 * `http`, `express`, and `ioredis`, whatever its configuration says afterwards.
 * It is also why the tests here can assert the disabled path without a Datadog
 * agent anywhere in sight.
 *
 * What the rest of the service gets is {@link Tracing}: three questions with
 * answers that are safe to use whether or not tracing is on.
 *
 * | Member          | Tracing on                                   | Tracing off |
 * |-----------------|----------------------------------------------|-------------|
 * | `traceContext()`| `{ dd: { trace_id, span_id } }` for the span | `undefined` |
 * | `traceHeaders()`| `x-datadog-*` propagation headers            | `{}`        |
 * | `sink`          | the tracer's DogStatsD client                | inert       |
 *
 * `traceHeaders()` is what puts the API request and the worker's processing of
 * the same job in one trace (Requirement 14.2): the headers travel in the message
 * envelope's `trace_context`, documented in `docs/message-schema.md`, and the
 * worker continues from them instead of starting a root span. Off, the envelope
 * carries `{}` and the worker starts a root span, which is the documented
 * behavior rather than a degraded one.
 */

import { parseBooleanFlag } from '../config/flags.js';
import { INERT_SINK } from './metrics.js';
import type { MetricsSink } from './metrics.js';

/** Datadog's propagation format for a text-map carrier. */
export const HTTP_HEADERS_FORMAT = 'http_headers';

export interface Tracing {
  /** True only when a tracer was actually loaded and initialized. */
  readonly enabled: boolean;
  /**
   * Log fields identifying the active span, shaped the way Datadog's log
   * correlation expects. Undefined when nothing is active, so the fields are
   * absent from the line rather than present and null.
   */
  traceContext(): Record<string, unknown> | undefined;
  /** Propagation headers for the active span; `{}` when there is none. */
  traceHeaders(): Record<string, string>;
  /** Where custom metrics go in addition to the Prometheus registry. */
  readonly sink: MetricsSink;
}

/** The disabled path: no tracer, no headers, no metric export. */
export const INERT_TRACING: Tracing = {
  enabled: false,
  traceContext: () => undefined,
  traceHeaders: () => ({}),
  sink: INERT_SINK,
};

/**
 * The slice of `dd-trace`'s surface this service uses. Declared structurally so
 * the loader can be faked in a test and so a `dd-trace` upgrade that changes an
 * unrelated part of its API cannot break the build here.
 */
export interface DatadogSpanContext {
  toTraceId(): string;
  toSpanId(): string;
}

export interface DatadogSpan {
  context(): DatadogSpanContext;
}

export interface DatadogScope {
  active(): DatadogSpan | null;
}

export interface DatadogTracer {
  init(options: Record<string, unknown>): unknown;
  scope(): DatadogScope;
  inject(context: DatadogSpanContext, format: string, carrier: Record<string, string>): void;
  /** Present when `dogstatsd` reporting is available; absent in older versions. */
  dogstatsd?: {
    increment(name: string, value?: number, tags?: Record<string, string>): void;
    gauge(name: string, value: number, tags?: Record<string, string>): void;
  };
}

/** Loads and returns the tracer module. Called at most once, and only if enabled. */
export type TracerLoader = () => DatadogTracer;

export interface TracingOptions {
  readonly enabled: boolean;
  readonly service: string;
  readonly env: string;
  readonly version: string | null;
  /** Reported on a failed load. Injectable so a test need not touch stderr. */
  readonly onLoadError?: (error: unknown) => void;
}

/**
 * Read the four variables the tracer needs straight from the environment.
 *
 * The configuration module is not used here on purpose: this runs before it can
 * be imported, because importing it pulls in the queue clients — exactly the
 * modules `dd-trace` must patch before they are loaded. An unrecognized
 * `OBSERVABILITY_ENABLED` value counts as off rather than fatal; there is no
 * logger at this point, and `loadConfig` is a few milliseconds away and will
 * refuse to start with the offending key named (Requirement 5.5).
 */
export function tracingOptionsFromEnv(env: NodeJS.ProcessEnv): TracingOptions {
  const nodeEnv = blankToUndefined(env['NODE_ENV']) ?? 'development';
  return {
    enabled: parseBooleanFlag(env['OBSERVABILITY_ENABLED']) === true,
    service: blankToUndefined(env['DD_SERVICE']) ?? 'publishhub-api',
    env: blankToUndefined(env['DD_ENV']) ?? nodeEnv,
    version: blankToUndefined(env['DD_VERSION']) ?? null,
  };
}

function blankToUndefined(value: string | undefined): string | undefined {
  const trimmed = value?.trim();
  return trimmed === undefined || trimmed === '' ? undefined : trimmed;
}

/**
 * Initialize tracing, or don't. Returns {@link INERT_TRACING} when the switch is
 * off — without calling the loader — and also when the loader throws: a missing
 * or broken APM library must not stop the service from serving traffic, which is
 * the whole argument for keeping observability optional.
 */
export function createTracing(options: TracingOptions, load: TracerLoader): Tracing {
  if (!options.enabled) {
    return INERT_TRACING;
  }

  let tracer: DatadogTracer;
  try {
    tracer = load();
    tracer.init({
      service: options.service,
      env: options.env,
      ...(options.version === null ? {} : { version: options.version }),
      // Log correlation is done by this service's own logger, which merges the
      // ids from `traceContext()`. Letting the tracer patch the logger as well
      // would produce two sets of trace fields on the same line.
      logInjection: false,
      // The metrics client is only useful with an agent to send to, and it is the
      // same switch: on together, off together.
      dogstatsd: true,
      runtimeMetrics: true,
    });
  } catch (error) {
    options.onLoadError?.(error);
    return INERT_TRACING;
  }

  const dogstatsd = tracer.dogstatsd;
  const sink: MetricsSink =
    dogstatsd === undefined
      ? INERT_SINK
      : {
          increment: (name, value, tags) => {
            dogstatsd.increment(name, value, tags);
          },
          gauge: (name, value, tags) => {
            dogstatsd.gauge(name, value, tags);
          },
        };

  function activeContext(): DatadogSpanContext | null {
    // A tracer that is initialized but has no span in scope is normal — startup
    // logs, shutdown logs, a scrape of `/metrics`. It is not an error condition,
    // so it produces no fields rather than a warning.
    const span = tracer.scope().active();
    return span === null ? null : span.context();
  }

  return {
    enabled: true,

    traceContext() {
      const context = activeContext();
      if (context === null) {
        return undefined;
      }
      return { dd: { trace_id: context.toTraceId(), span_id: context.toSpanId() } };
    },

    traceHeaders() {
      const context = activeContext();
      if (context === null) {
        return {};
      }
      const carrier: Record<string, string> = {};
      tracer.inject(context, HTTP_HEADERS_FORMAT, carrier);
      // The carrier is passed through as the tracer wrote it. The envelope treats
      // these keys as opaque (docs/message-schema.md), so filtering them here
      // would only risk dropping a key a future propagation style needs.
      return carrier;
    },

    sink,
  };
}
