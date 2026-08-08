/**
 * The observability switch, end to end (Requirements 14.6, 14.2, 14.3).
 *
 * One requirement, two directions:
 *
 * - **Off** — the service runs exactly as it did before observability existed.
 *   `dd-trace` is never loaded, log lines carry no trace fields, the envelope's
 *   `trace_context` is `{}`, and `/metrics` still answers. Local development needs
 *   no Datadog account and no agent.
 * - **On** — the envelope carries the Datadog propagation headers, so the worker's
 *   span continues this request's trace, and the log lines carry the same ids.
 *
 * Both directions run against the real app over HTTP with a fake tracer, so the
 * suite proves the wiring without ever loading an APM library.
 */

import { createRequire } from 'node:module';

import { afterEach, describe, expect, it } from 'vitest';

import { INERT_TRACING, createTracing } from '../tracing.js';
import type { Tracing } from '../tracing.js';
import { FakeDatadogTracer, fakeSpan } from './fake-tracer.js';
import { publish, scrape, startApi } from './harness.js';
import type { ApiHarness } from './harness.js';

const running: ApiHarness[] = [];

afterEach(async () => {
  await Promise.all(running.splice(0).map((api) => api.close()));
});

async function start(...args: Parameters<typeof startApi>): Promise<ApiHarness> {
  const api = await startApi(...args);
  running.push(api);
  return api;
}

const TRACE_ID = '6249442685991245312';
const SPAN_ID = '8114249130118331704';

/** Tracing as the entrypoint would have built it, minus the real library. */
function activeTracing(): Tracing {
  const tracer = new FakeDatadogTracer();
  tracer.activeSpan = fakeSpan(TRACE_ID, SPAN_ID);
  return createTracing(
    { enabled: true, service: 'publishhub-api', env: 'prod', version: '1.4.2' },
    () => tracer,
  );
}

describe('observability disabled', () => {
  it('leaves dd-trace out of the process entirely', async () => {
    await start();

    // `createRequire` shares Node's CommonJS module cache, so a `dd-trace` that
    // had been loaded anywhere in this worker would show up here.
    const loaded = Object.keys(createRequire(import.meta.url).cache);
    expect(loaded.filter((path) => path.includes('dd-trace'))).toEqual([]);
  });

  it('enqueues an empty trace_context, so the worker starts a root span', async () => {
    const api = await start();

    const response = await publish(api, {
      content: 'Shipping PublishHub.',
      platforms: ['twitter'],
    });

    expect(response.status).toBe(202);
    expect(api.queue.enqueued).toHaveLength(1);
    // Empty rather than absent or null — that is the documented contract
    // (docs/message-schema.md).
    expect(api.queue.enqueued[0]?.trace_context).toEqual({});
  });

  it('writes log lines with no trace fields', async () => {
    const api = await start();

    await publish(api, { content: 'Shipping PublishHub.', platforms: ['twitter'] });

    const lines = await api.capture.waitFor(2);
    for (const line of lines) {
      expect(line).not.toHaveProperty('dd');
      expect(line).not.toHaveProperty('trace_id');
    }
    // And the fields that must always be there still are (Requirement 14.3).
    expect(lines[0]).toMatchObject({ service: 'publishhub-api', env: 'development' });
  });

  it('still serves /metrics', async () => {
    const api = await start();

    const { status, body } = await scrape(api);

    expect(status).toBe(200);
    expect(body).toContain('publishhub_posts_submitted_total');
  });

  it('answers requests identically to an app built with an inert tracing seam', async () => {
    // The switch changes what is recorded, never what a client is told.
    const withoutTracing = await start();
    const withInertTracing = await start({ tracing: INERT_TRACING });

    const [a, b] = await Promise.all([
      publish(withoutTracing, { content: 'Shipping PublishHub.', platforms: ['twitter'] }),
      publish(withInertTracing, { content: 'Shipping PublishHub.', platforms: ['twitter'] }),
    ]);

    expect(a.status).toBe(b.status);
    expect(a.headers.get('content-type')).toBe(b.headers.get('content-type'));
    expect(withoutTracing.queue.enqueued[0]?.trace_context).toEqual(
      withInertTracing.queue.enqueued[0]?.trace_context,
    );
  });
});

describe('observability enabled', () => {
  it('carries the Datadog propagation headers in the envelope', async () => {
    const api = await start({ tracing: activeTracing() });

    const response = await publish(api, {
      content: 'Shipping PublishHub.',
      platforms: ['twitter'],
    });

    expect(response.status).toBe(202);
    expect(api.queue.enqueued[0]?.trace_context).toEqual({
      'x-datadog-trace-id': TRACE_ID,
      'x-datadog-parent-id': SPAN_ID,
      'x-datadog-sampling-priority': '1',
    });
  });

  it('tags log lines with the same trace ids the envelope carries', async () => {
    const api = await start({ tracing: activeTracing() });

    await publish(api, { content: 'Shipping PublishHub.', platforms: ['twitter'] });

    const lines = await api.capture.waitFor(2);
    for (const line of lines) {
      expect(line.dd).toEqual({ trace_id: TRACE_ID, span_id: SPAN_ID });
    }
  });

  it('keeps the envelope valid, so an enabled tracer cannot break publishing', async () => {
    const api = await start({ tracing: activeTracing() });

    const response = await publish(api, {
      content: 'Shipping PublishHub.',
      platforms: ['twitter', 'linkedin'],
    });
    const { id } = (await response.json()) as { id: string };

    const [job] = api.queue.enqueued;
    expect(job).toMatchObject({ post_id: id, platforms: ['twitter', 'linkedin'], attempt: 1 });
  });
});
