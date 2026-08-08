/**
 * Health and readiness tests (Requirements 2.3, 2.4).
 *
 * Driven through the real app on an ephemeral port, because the behavior under
 * test is HTTP-shaped: status codes, headers, and whether a handler touched a
 * dependency at all. Calling the router functions directly would assert the
 * wiring of a mock instead.
 *
 * The fakes count their own calls, which is what makes the central claim
 * checkable: `/health` must reach zero dependencies, no matter their state.
 */

import type { AddressInfo } from 'node:net';

import { afterEach, describe, expect, it } from 'vitest';

import { createApp } from '../../app.js';
import { loadConfig } from '../../config/index.js';
import { createLogger } from '../../logging/index.js';
import { createLogCapture } from '../../logging/testing/log-capture.js';
import type { LogCapture } from '../../logging/testing/log-capture.js';
import { FakeQueueClient } from '../../queue/testing/fake-queue-client.js';
import { FakeRedis } from '../../queue/testing/fake-redis.js';
import { DEPENDENCY_UNAVAILABLE } from '../health.js';

/**
 * Redis stand-in whose reachability the test controls. The in-memory fake
 * supplies the hash and list commands the app's post store needs; only `PING`,
 * the one command readiness uses, is overridden here.
 */
class FakeRedisProbe extends FakeRedis {
  pingCalls = 0;
  behavior: 'up' | 'down' | 'hang' = 'up';

  override async ping(): Promise<'PONG'> {
    this.pingCalls += 1;
    if (this.behavior === 'down') {
      // Shape and wording of a real ioredis connection failure, including the
      // credentials a naive handler would echo into the response body.
      throw new Error('connect ECONNREFUSED 127.0.0.1:6379 (redis://user:s3cret@127.0.0.1:6379)');
    }
    if (this.behavior === 'hang') {
      return new Promise<'PONG'>(() => {
        /* never settles, like a socket waiting on a black-holed dependency */
      });
    }
    return 'PONG';
  }
}

/** Queue stand-in. `depth()` is the reachability probe for both backends. */
class FakeQueueProbe extends FakeQueueClient {
  calls = 0;
  behavior: 'up' | 'down' | 'hang' = 'up';

  override async depth(): Promise<number> {
    this.calls += 1;
    if (this.behavior === 'down') {
      throw new Error('QueueDoesNotExist: the specified queue does not exist');
    }
    if (this.behavior === 'hang') {
      return new Promise<number>(() => {
        /* never settles */
      });
    }
    return 3;
  }
}

interface Harness {
  readonly url: string;
  readonly redis: FakeRedisProbe;
  readonly queue: FakeQueueProbe;
  readonly capture: LogCapture;
  close(): Promise<void>;
}

const harnesses: Harness[] = [];

afterEach(async () => {
  await Promise.all(harnesses.splice(0).map((harness) => harness.close()));
});

async function startApi(
  options: { readinessTimeoutMs?: number; env?: Record<string, string> } = {},
): Promise<Harness> {
  const capture = createLogCapture();
  const config = loadConfig(options.env ?? {});
  const logger = createLogger(config, { destination: capture.stream });
  const redis = new FakeRedisProbe();
  const queue = new FakeQueueProbe();

  const app = createApp({
    config,
    logger,
    redis,
    queue,
    ...(options.readinessTimeoutMs === undefined
      ? {}
      : { readinessTimeoutMs: options.readinessTimeoutMs }),
  });

  const server = app.listen(0);
  await new Promise<void>((resolve, reject) => {
    server.once('listening', resolve);
    server.once('error', reject);
  });
  const { port } = server.address() as AddressInfo;

  const harness: Harness = {
    url: `http://127.0.0.1:${String(port)}`,
    redis,
    queue,
    capture,
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.close((error) => {
          if (error) {
            reject(error);
          } else {
            resolve();
          }
        });
      }),
  };
  harnesses.push(harness);
  return harness;
}

describe('GET /health', () => {
  it('returns 200 with process liveness information', async () => {
    const api = await startApi();

    const response = await fetch(`${api.url}/health`);
    const body = (await response.json()) as Record<string, unknown>;

    expect(response.status).toBe(200);
    expect(body).toMatchObject({
      status: 'ok',
      service: 'publishhub-api',
      env: 'development',
      pid: process.pid,
    });
    expect(typeof body.uptime_s).toBe('number');
    expect(body.uptime_s as number).toBeGreaterThan(0);
  });

  it('never touches Redis or the queue', async () => {
    const api = await startApi();

    await fetch(`${api.url}/health`);
    await fetch(`${api.url}/health`);

    expect(api.redis.pingCalls).toBe(0);
    expect(api.queue.calls).toBe(0);
  });

  it('stays 200 while both dependencies are unreachable', async () => {
    // The design's edge case: a pod waiting on Redis must not be restarted for
    // it. Liveness reflects the process, readiness reflects dependencies.
    const api = await startApi();
    api.redis.behavior = 'down';
    api.queue.behavior = 'down';

    const response = await fetch(`${api.url}/health`);

    expect(response.status).toBe(200);
    expect(api.redis.pingCalls).toBe(0);
  });

  it('is not cacheable', async () => {
    const api = await startApi();

    const response = await fetch(`${api.url}/health`);

    expect(response.headers.get('cache-control')).toBe('no-store');
  });
});

describe('GET /ready', () => {
  it('returns 200 when Redis and the queue both answer', async () => {
    const api = await startApi();

    const response = await fetch(`${api.url}/ready`);
    const body = (await response.json()) as Record<string, unknown>;

    expect(response.status).toBe(200);
    expect(body).toMatchObject({
      status: 'ready',
      checks: {
        redis: { status: 'ok' },
        queue: { status: 'ok', backend: 'redis' },
      },
    });
    expect(body).not.toHaveProperty('error');
    expect(api.redis.pingCalls).toBe(1);
    expect(api.queue.calls).toBe(1);
  });

  it('returns 503 with the error envelope when Redis is unreachable', async () => {
    const api = await startApi();
    api.redis.behavior = 'down';

    const response = await fetch(`${api.url}/ready`);
    const body = (await response.json()) as {
      status: string;
      checks: { redis: { status: string }; queue: { status: string } };
      error: { code: string; message: string; request_id: string };
    };

    expect(response.status).toBe(503);
    expect(body.status).toBe('unready');
    expect(body.checks.redis.status).toBe('error');
    // The queue is still checked and still reported: knowing whether one or both
    // dependencies are down is the difference between a Redis incident and a
    // network partition.
    expect(body.checks.queue.status).toBe('ok');
    expect(body.error.code).toBe(DEPENDENCY_UNAVAILABLE);
    expect(body.error.message).toContain('redis');
    expect(body.error.request_id).toBe(response.headers.get('x-request-id'));
  });

  it('returns 503 when the queue is unreachable', async () => {
    const api = await startApi();
    api.queue.behavior = 'down';

    const response = await fetch(`${api.url}/ready`);
    const body = (await response.json()) as {
      checks: { redis: { status: string }; queue: { status: string } };
      error: { code: string; message: string };
    };

    expect(response.status).toBe(503);
    expect(body.checks.redis.status).toBe('ok');
    expect(body.checks.queue.status).toBe('error');
    expect(body.error.message).toContain('queue');
  });

  it('returns 503 naming both dependencies when both are unreachable', async () => {
    const api = await startApi();
    api.redis.behavior = 'down';
    api.queue.behavior = 'down';

    const response = await fetch(`${api.url}/ready`);
    const body = (await response.json()) as { error: { message: string } };

    expect(response.status).toBe(503);
    expect(body.error.message).toContain('redis');
    expect(body.error.message).toContain('queue');
  });

  it('returns 503 rather than hanging when a dependency never answers', async () => {
    const api = await startApi({ readinessTimeoutMs: 30 });
    api.redis.behavior = 'hang';

    const response = await fetch(`${api.url}/ready`);
    const body = (await response.json()) as {
      checks: { redis: { status: string; duration_ms: number }; queue: { status: string } };
    };

    expect(response.status).toBe(503);
    expect(body.checks.redis.status).toBe('error');
    expect(body.checks.redis.duration_ms).toBeGreaterThan(0);
    // The healthy dependency is not held hostage by the stalled one: the checks
    // run concurrently.
    expect(body.checks.queue.status).toBe('ok');
  });

  it('logs the underlying failure server-side and keeps it out of the response', async () => {
    const api = await startApi();
    api.redis.behavior = 'down';

    const response = await fetch(`${api.url}/ready`);
    const raw = await response.text();

    // Requirement 2.7's spirit applied to a 503: the client learns which
    // dependency is down, not the connection string it failed to reach.
    expect(raw).not.toContain('s3cret');
    expect(raw).not.toContain('ECONNREFUSED');

    const lines = await api.capture.waitFor(2);
    const failure = lines.find((line) => line.msg === 'readiness check failed');
    expect(failure).toMatchObject({ level: 'error', dependency: 'redis' });
    expect((failure?.err as { message?: string }).message).toContain('ECONNREFUSED');

    // Matched on the fields rather than the message: pino-http words a 5xx
    // completion line differently from a 2xx one.
    const requestLine = lines.find((line) => line.path === '/ready');
    expect(requestLine).toMatchObject({ level: 'error', status_code: 503 });
    // Same correlation id on the log line and in the envelope, so a reported
    // 503 leads straight to its cause.
    expect(requestLine?.correlation_id).toBe(response.headers.get('x-request-id'));
  });

  it('reports the sqs backend when that is the configured queue', async () => {
    const api = await startApi({
      env: {
        QUEUE_BACKEND: 'sqs',
        SQS_QUEUE_URL: 'https://sqs.us-east-1.amazonaws.com/123456789012/publishhub-jobs',
      },
    });

    const response = await fetch(`${api.url}/ready`);
    const body = (await response.json()) as { checks: { queue: { backend: string } } };

    expect(response.status).toBe(200);
    expect(body.checks.queue.backend).toBe('sqs');
  });

  it('is not cacheable', async () => {
    const api = await startApi();

    const response = await fetch(`${api.url}/ready`);

    expect(response.headers.get('cache-control')).toBe('no-store');
  });
});
