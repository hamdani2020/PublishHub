/**
 * A running API on an ephemeral port, with in-memory Redis and queue fakes.
 *
 * The observability tests drive real HTTP for the same reason the health and
 * publish tests do: what is under test is what a scrape and a client actually see
 * — the exposition body, the response headers, the log lines the request wrote,
 * and what ended up on the queue. None of that is observable from a fake `res`.
 */

import type { AddressInfo } from 'node:net';
import type { Server } from 'node:http';

import { createApp } from '../../app.js';
import { loadConfig } from '../../config/index.js';
import type { ApiConfig } from '../../config/index.js';
import { createLogger } from '../../logging/index.js';
import { createLogCapture } from '../../logging/testing/log-capture.js';
import type { LogCapture } from '../../logging/testing/log-capture.js';
import { FakeQueueClient } from '../../queue/testing/fake-queue-client.js';
import { FakeRedis } from '../../queue/testing/fake-redis.js';
import type { Metrics } from '../metrics.js';
import type { Tracing } from '../tracing.js';

export interface ApiHarness {
  readonly url: string;
  readonly config: ApiConfig;
  readonly redis: FakeRedis;
  readonly queue: FakeQueueClient;
  readonly capture: LogCapture;
  close(): Promise<void>;
}

export interface HarnessOptions {
  /** Environment handed to `loadConfig`, so a test can flip `DD_*` and friends. */
  readonly env?: NodeJS.ProcessEnv | Record<string, string>;
  readonly metrics?: Metrics;
  readonly tracing?: Tracing;
}

export async function startApi(options: HarnessOptions = {}): Promise<ApiHarness> {
  const capture = createLogCapture();
  const config = loadConfig(options.env ?? {});
  const logger = createLogger(config, {
    destination: capture.stream,
    // Wired the way the entrypoint wires it: the provider is always passed, and
    // an inert tracing seam simply never returns any fields.
    ...(options.tracing === undefined
      ? {}
      : { traceContext: () => options.tracing?.traceContext() }),
  });
  const redis = new FakeRedis();
  const queue = new FakeQueueClient();

  const app = createApp({
    config,
    logger,
    redis,
    queue,
    ...(options.metrics === undefined ? {} : { metrics: options.metrics }),
    ...(options.tracing === undefined ? {} : { tracing: options.tracing }),
  });

  const server: Server = app.listen(0);
  await new Promise<void>((resolve, reject) => {
    server.once('listening', resolve);
    server.once('error', reject);
  });
  const { port } = server.address() as AddressInfo;

  return {
    url: `http://127.0.0.1:${String(port)}`,
    config,
    redis,
    queue,
    capture,
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections();
        server.close(() => {
          resolve();
        });
      }),
  };
}

export async function publish(api: ApiHarness, body: unknown): Promise<Response> {
  return fetch(`${api.url}/api/v1/publish`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function scrape(api: ApiHarness): Promise<{ status: number; contentType: string | null; body: string }> {
  const response = await fetch(`${api.url}/metrics`);
  return {
    status: response.status,
    contentType: response.headers.get('content-type'),
    body: await response.text(),
  };
}
