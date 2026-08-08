/**
 * Tracing seam tests (Requirements 14.2, 14.6).
 *
 * The claim worth testing is not "dd-trace works" — that is Datadog's test suite.
 * It is that this service decides *once* whether to load it, that the disabled
 * path never touches it, and that when it is active the propagation headers and
 * the log fields come out in the documented shape.
 */

import { describe, expect, it, vi } from 'vitest';

import { INERT_SINK, METRICS } from '../metrics.js';
import { HTTP_HEADERS_FORMAT, INERT_TRACING, createTracing, tracingOptionsFromEnv } from '../tracing.js';
import type { TracingOptions } from '../tracing.js';
import { FakeDatadogTracer, fakeSpan } from './fake-tracer.js';

const ENABLED: TracingOptions = {
  enabled: true,
  service: 'publishhub-api',
  env: 'prod',
  version: '1.4.2',
};

describe('createTracing — observability disabled', () => {
  it('never calls the loader', () => {
    const load = vi.fn(() => new FakeDatadogTracer());

    const tracing = createTracing({ ...ENABLED, enabled: false }, load);

    expect(load).not.toHaveBeenCalled();
    expect(tracing).toBe(INERT_TRACING);
  });

  it('reports no trace context, no headers, and an inert sink', () => {
    const tracing = createTracing({ ...ENABLED, enabled: false }, () => new FakeDatadogTracer());

    expect(tracing.enabled).toBe(false);
    expect(tracing.traceContext()).toBeUndefined();
    expect(tracing.traceHeaders()).toEqual({});
    expect(tracing.sink).toBe(INERT_SINK);
  });
});

describe('createTracing — observability enabled', () => {
  it('initializes the tracer once with the service, environment, and version', () => {
    const tracer = new FakeDatadogTracer();

    const tracing = createTracing(ENABLED, () => tracer);

    expect(tracing.enabled).toBe(true);
    expect(tracer.initCalls).toBe(1);
    expect(tracer.initOptions).toMatchObject({
      service: 'publishhub-api',
      env: 'prod',
      version: '1.4.2',
      // The logger merges the ids itself; letting the tracer patch pino as well
      // would put two sets of trace fields on one line.
      logInjection: false,
    });
  });

  it('omits version when the build does not stamp one', () => {
    const tracer = new FakeDatadogTracer();

    createTracing({ ...ENABLED, version: null }, () => tracer);

    expect(tracer.initOptions).not.toHaveProperty('version');
  });

  it('injects Datadog propagation headers for the active span', () => {
    const tracer = new FakeDatadogTracer();
    tracer.activeSpan = fakeSpan('6249442685991245312', '8114249130118331704');

    const headers = createTracing(ENABLED, () => tracer).traceHeaders();

    expect(headers).toEqual({
      'x-datadog-trace-id': '6249442685991245312',
      'x-datadog-parent-id': '8114249130118331704',
      'x-datadog-sampling-priority': '1',
    });
    expect(tracer.injections).toEqual([HTTP_HEADERS_FORMAT]);
  });

  it('reports the span ids as log fields', () => {
    const tracer = new FakeDatadogTracer();
    tracer.activeSpan = fakeSpan('6249442685991245312', '8114249130118331704');

    expect(createTracing(ENABLED, () => tracer).traceContext()).toEqual({
      dd: { trace_id: '6249442685991245312', span_id: '8114249130118331704' },
    });
  });

  it('reports nothing while no span is in scope', () => {
    // Startup lines, shutdown lines, and a metrics scrape all land here. Not an
    // error condition, so it produces no fields rather than a warning.
    const tracer = new FakeDatadogTracer();
    tracer.activeSpan = null;

    const tracing = createTracing(ENABLED, () => tracer);

    expect(tracing.traceHeaders()).toEqual({});
    expect(tracing.traceContext()).toBeUndefined();
    expect(tracer.injections).toEqual([]);
  });

  it('forwards custom metrics to the tracer dogstatsd client', () => {
    const tracer = new FakeDatadogTracer();

    const { sink } = createTracing(ENABLED, () => tracer);
    sink.increment(METRICS.postsSubmitted.datadog, 1, { platform: 'twitter' });
    sink.gauge(METRICS.queueDepth.datadog, 7, { backend: 'redis' });

    expect(tracer.increments).toEqual([
      { name: 'publishhub.posts.submitted', value: 1, tags: { platform: 'twitter' } },
    ]);
    expect(tracer.gauges).toEqual([
      { name: 'publishhub.queue.depth', value: 7, tags: { backend: 'redis' } },
    ]);
  });

  it('falls back to inert rather than failing when the tracer cannot load', () => {
    // A missing or broken APM library must not stop the service from serving
    // traffic — that is the whole argument for keeping observability optional.
    const onLoadError = vi.fn();

    const tracing = createTracing({ ...ENABLED, onLoadError }, () => {
      throw new Error('Cannot find module dd-trace');
    });

    expect(tracing).toBe(INERT_TRACING);
    expect(onLoadError).toHaveBeenCalledTimes(1);
  });
});

describe('tracingOptionsFromEnv', () => {
  it('is disabled when the switch is absent', () => {
    expect(tracingOptionsFromEnv({})).toMatchObject({
      enabled: false,
      service: 'publishhub-api',
      env: 'development',
      version: null,
    });
  });

  it('accepts every documented spelling of the switch', () => {
    for (const value of ['true', 'TRUE', '1', 'yes', 'on']) {
      expect(tracingOptionsFromEnv({ OBSERVABILITY_ENABLED: value }).enabled, value).toBe(true);
    }
    for (const value of ['false', '0', 'no', 'off', '', '  ', 'maybe']) {
      // An unrecognized value counts as off here rather than fatal: there is no
      // logger yet, and `loadConfig` refuses to start moments later with the key
      // named (Requirement 5.5).
      expect(tracingOptionsFromEnv({ OBSERVABILITY_ENABLED: value }).enabled, value).toBe(false);
    }
  });

  it('falls back from DD_ENV to NODE_ENV and reads service and version', () => {
    expect(
      tracingOptionsFromEnv({
        OBSERVABILITY_ENABLED: 'true',
        NODE_ENV: 'production',
        DD_SERVICE: 'publishhub-api-canary',
        DD_VERSION: '1.4.2',
      }),
    ).toEqual({
      enabled: true,
      service: 'publishhub-api-canary',
      env: 'production',
      version: '1.4.2',
    });

    expect(
      tracingOptionsFromEnv({ NODE_ENV: 'production', DD_ENV: 'prod' }).env,
    ).toBe('prod');
  });
});
