/**
 * A `dd-trace` stand-in.
 *
 * Every test in this package loads this instead of the real library, which is the
 * point of the loader seam: the assertions are about what the service does with a
 * tracer, and none of them need an APM agent, a socket, or a monkey-patched
 * `http` module.
 *
 * `inject` writes the three headers `docs/message-schema.md` documents, in the
 * same shape Datadog's HTTP propagator produces, so a test asserting on the
 * envelope is asserting on the real key names.
 */

import type { DatadogSpan, DatadogSpanContext, DatadogTracer } from '../tracing.js';

export function fakeSpan(traceId: string, spanId: string): DatadogSpan {
  const context: DatadogSpanContext = {
    toTraceId: () => traceId,
    toSpanId: () => spanId,
  };
  return { context: () => context };
}

export interface RecordedMetric {
  readonly name: string;
  readonly value: number;
  readonly tags: Record<string, string> | undefined;
}

export class FakeDatadogTracer implements DatadogTracer {
  /** Options `init` was called with, or null when it never was. */
  initOptions: Record<string, unknown> | null = null;
  initCalls = 0;
  /** What `scope().active()` reports. Null means "no span in scope". */
  activeSpan: DatadogSpan | null = null;
  readonly injections: string[] = [];
  readonly increments: RecordedMetric[] = [];
  readonly gauges: RecordedMetric[] = [];

  readonly dogstatsd = {
    increment: (name: string, value?: number, tags?: Record<string, string>): void => {
      this.increments.push({ name, value: value ?? 1, tags });
    },
    gauge: (name: string, value: number, tags?: Record<string, string>): void => {
      this.gauges.push({ name, value, tags });
    },
  };

  init(options: Record<string, unknown>): this {
    this.initOptions = options;
    this.initCalls += 1;
    return this;
  }

  scope(): { active(): DatadogSpan | null } {
    return { active: () => this.activeSpan };
  }

  inject(context: DatadogSpanContext, format: string, carrier: Record<string, string>): void {
    this.injections.push(format);
    carrier['x-datadog-trace-id'] = context.toTraceId();
    carrier['x-datadog-parent-id'] = context.toSpanId();
    carrier['x-datadog-sampling-priority'] = '1';
  }
}
