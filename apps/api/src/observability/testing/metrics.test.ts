/**
 * Custom metrics and the `/metrics` endpoint (Requirements 14.4, 14.6).
 *
 * Two levels, on purpose. The recorder is tested directly, because label sets and
 * the dotted-to-underscored name mapping are easiest to pin down there. The
 * endpoint is tested through the running app, because a scrape is HTTP: the
 * content type matters, the exposition text matters, and "does a submitted post
 * show up in the numbers" is only a real answer if the request went through the
 * same middleware stack the container runs.
 */

import { afterEach, describe, expect, it } from 'vitest';

import { loadConfig } from '../../config/index.js';
import { METRICS, createMetrics } from '../metrics.js';
import type { MetricsSink } from '../metrics.js';
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

interface Recorded {
  readonly name: string;
  readonly value: number;
  readonly tags: Record<string, string>;
}

function recordingSink(): { sink: MetricsSink; counts: Recorded[]; gauges: Recorded[] } {
  const counts: Recorded[] = [];
  const gauges: Recorded[] = [];
  return {
    sink: {
      increment: (name, value, tags) => counts.push({ name, value, tags }),
      gauge: (name, value, tags) => gauges.push({ name, value, tags }),
    },
    counts,
    gauges,
  };
}

/** Value of one Prometheus series, or undefined when the series is absent. */
function series(body: string, name: string, labels: string): number | undefined {
  const line = body
    .split('\n')
    .find((candidate) => candidate.startsWith(`${name}{${labels}}`));
  return line === undefined ? undefined : Number(line.slice(line.lastIndexOf(' ') + 1));
}

describe('createMetrics', () => {
  it('counts a submitted post once per requested platform', async () => {
    const metrics = createMetrics({ config: loadConfig({}) });

    metrics.postSubmitted(['twitter', 'linkedin'], 'queued');

    const { body } = await metrics.render();
    expect(
      series(body, METRICS.postsSubmitted.prometheus, 'platform="twitter",status="queued",env="development"'),
    ).toBe(1);
    expect(
      series(body, METRICS.postsSubmitted.prometheus, 'platform="linkedin",status="queued",env="development"'),
    ).toBe(1);
  });

  it('tags every series with the Datadog environment', async () => {
    const metrics = createMetrics({
      config: loadConfig({ NODE_ENV: 'production', CORS_ORIGINS: 'https://app.example.com', DD_ENV: 'prod' }),
    });

    metrics.postSubmitted(['twitter'], 'queued');
    metrics.queueDepthObserved('sqs', 3);

    const { body } = await metrics.render();
    expect(series(body, METRICS.queueDepth.prometheus, 'backend="sqs",env="prod"')).toBe(3);
    expect(
      series(body, METRICS.postsSubmitted.prometheus, 'platform="twitter",status="queued",env="prod"'),
    ).toBe(1);
  });

  it('counts a 5xx on both the request and the error counter', async () => {
    const metrics = createMetrics({ config: loadConfig({}) });

    metrics.requestObserved({ method: 'POST', statusCode: 202 });
    metrics.requestObserved({ method: 'POST', statusCode: 503 });

    const { body } = await metrics.render();
    const labels = 'method="POST",status_code="503",env="development"';
    expect(series(body, METRICS.apiRequests.prometheus, labels)).toBe(1);
    expect(series(body, METRICS.apiErrors.prometheus, labels)).toBe(1);
    // A 202 is a request and nothing more.
    expect(
      series(body, METRICS.apiErrors.prometheus, 'method="POST",status_code="202",env="development"'),
    ).toBeUndefined();
  });

  it('records the queue depth as a gauge that replaces its previous value', async () => {
    const metrics = createMetrics({ config: loadConfig({}) });

    metrics.queueDepthObserved('redis', 12);
    metrics.queueDepthObserved('redis', 4);

    const { body } = await metrics.render();
    expect(series(body, METRICS.queueDepth.prometheus, 'backend="redis",env="development"')).toBe(4);
  });

  it('forwards every recording to the Datadog sink under its dotted name', () => {
    const { sink, counts, gauges } = recordingSink();
    const metrics = createMetrics({ config: loadConfig({}), sink });

    metrics.postSubmitted(['twitter'], 'queued');
    metrics.requestObserved({ method: 'GET', statusCode: 500 });
    metrics.queueDepthObserved('redis', 2);

    expect(counts.map((entry) => entry.name)).toEqual([
      METRICS.postsSubmitted.datadog,
      METRICS.apiRequests.datadog,
      METRICS.apiErrors.datadog,
    ]);
    expect(counts[0]?.tags).toEqual({ platform: 'twitter', status: 'queued', env: 'development' });
    expect(gauges).toEqual([
      { name: METRICS.queueDepth.datadog, value: 2, tags: { backend: 'redis', env: 'development' } },
    ]);
  });

  it('records into its own registry, so two apps in one process do not collide', () => {
    const config = loadConfig({});

    // A shared global registry would throw here on the second registration.
    expect(() => {
      createMetrics({ config });
      createMetrics({ config });
    }).not.toThrow();
  });
});

describe('GET /metrics', () => {
  it('serves Prometheus exposition with every metric the API owns', async () => {
    const api = await start();

    const { status, contentType, body } = await scrape(api);

    expect(status).toBe(200);
    expect(contentType).toContain('text/plain');
    for (const metric of [METRICS.postsSubmitted, METRICS.apiRequests, METRICS.apiErrors, METRICS.queueDepth]) {
      expect(body).toContain(`# TYPE ${metric.prometheus}`);
    }
  });

  it('does not emit the worker-owned job metrics', async () => {
    // `publishhub.jobs.*` describes work this service never does. Emitting a zero
    // for it from here would make a broken worker look like an idle one.
    const api = await start();

    const { body } = await scrape(api);

    expect(body).not.toContain('publishhub_jobs_processed');
    expect(body).not.toContain('publishhub_jobs_failed');
    expect(body).not.toContain('publishhub_jobs_duration');
  });

  it('reflects a submitted post, per platform', async () => {
    const api = await start();

    const accepted = await publish(api, {
      content: 'Shipping PublishHub.',
      platforms: ['twitter', 'linkedin'],
    });
    expect(accepted.status).toBe(202);

    const { body } = await scrape(api);
    expect(
      series(body, METRICS.postsSubmitted.prometheus, 'platform="twitter",status="queued",env="development"'),
    ).toBe(1);
    expect(
      series(body, METRICS.postsSubmitted.prometheus, 'platform="linkedin",status="queued",env="development"'),
    ).toBe(1);
    expect(
      series(body, METRICS.apiRequests.prometheus, 'method="POST",status_code="202",env="development"'),
    ).toBe(1);
  });

  it('counts a rejected submission separately from an accepted one', async () => {
    const api = await start();

    await publish(api, { content: '', platforms: ['twitter'] });

    const { body } = await scrape(api);
    expect(
      series(body, METRICS.postsSubmitted.prometheus, 'platform="none",status="rejected",env="development"'),
    ).toBe(1);
    expect(
      series(body, METRICS.apiRequests.prometheus, 'method="POST",status_code="400",env="development"'),
    ).toBe(1);
  });

  it('samples the queue depth at scrape time', async () => {
    const api = await start();

    await publish(api, { content: 'Shipping PublishHub.', platforms: ['twitter'] });

    const { body } = await scrape(api);
    expect(series(body, METRICS.queueDepth.prometheus, 'backend="redis",env="development"')).toBe(1);
  });

  it('still answers when the queue cannot report its depth', async () => {
    // A monitoring endpoint that goes down with its dependencies is the one that
    // cannot tell you why they went down.
    const api = await start();
    api.queue.depth = () => Promise.reject(new Error('connect ECONNREFUSED 127.0.0.1:6379'));

    const { status, body } = await scrape(api);

    expect(status).toBe(200);
    expect(body).toContain(`# TYPE ${METRICS.apiRequests.prometheus}`);
  });

  it('does not count its own scrapes as traffic', async () => {
    const api = await start();

    await scrape(api);
    const { body } = await scrape(api);

    expect(
      series(body, METRICS.apiRequests.prometheus, 'method="GET",status_code="200",env="development"'),
    ).toBeUndefined();
  });
});
