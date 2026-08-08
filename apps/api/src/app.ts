/**
 * Express application factory.
 *
 * `createApp` builds the app and returns it without listening. Binding a port,
 * installing signal handlers, and constructing the real Redis and queue clients
 * belong to the process entrypoint (spec task 3.6), which keeps this factory
 * usable from a test that starts the app on an ephemeral port and shuts it down
 * again.
 *
 * Every collaborator is injected. Nothing in here reads `process.env` or opens a
 * socket, so the unit tests exercise the same wiring the container runs, with
 * fakes in place of Redis and the queue.
 *
 * Middleware order is deliberate:
 *
 * 1. Request logging, so every request produces exactly one line — including the
 *    ones that fail before reaching a handler (Requirement 2.6).
 * 2. Security headers and CORS, ahead of everything that answers. A response that
 *    skipped them would be the one response an attacker cares about.
 * 3. Health, readiness, and `/metrics` at the root, ahead of body parsing and the
 *    API routes. Probes and scrapes should not pay for anything they do not use.
 * 4. The `/api/v1` routes, which parse bodies (size-limited by `JSON_BODY_LIMIT`,
 *    applied where the parsing happens) and talk to Redis and the queue. Publish
 *    first, then the read-only post queries; the paths do not overlap, so the
 *    order is for readability rather than for correctness.
 * 5. The 404 handler, which turns an unmatched path into the standard envelope
 *    rather than Express's HTML page.
 * 6. The central error handler, last of all, so every throw from anything above
 *    becomes a generic 500 with the error in the log and not in the body
 *    (Requirement 2.7).
 *
 * The request counter sits alongside the request logger, high enough to see the
 * status code of everything the app answers. Tracing arrives as the injected
 * `Tracing` seam and defaults to inert, so an app built without it behaves exactly
 * as it did before observability existed (Requirement 14.6).
 */

import express from 'express';
import type { Express } from 'express';
import type { Logger } from 'pino';

import type { ApiConfig } from './config/index.js';
import { createHealthRouter } from './health/index.js';
import type { RedisProbe } from './health/index.js';
import {
  createCors,
  createErrorHandler,
  createNotFoundHandler,
  createSecurityHeaders,
} from './http/index.js';
import { createRequestLogger } from './logging/index.js';
import {
  INERT_TRACING,
  createMetrics,
  createMetricsRouter,
  createRequestMetrics,
} from './observability/index.js';
import type { Metrics, Tracing } from './observability/index.js';
import { RedisPostStore, createPublishRouter, createQueryRouter } from './posts/index.js';
import type { PostStoreCommands } from './posts/index.js';
import type { QueueClient } from './queue/index.js';

/**
 * What the app needs from Redis: reachability for the readiness probe, plus the
 * hash and list commands the post store writes with. `ioredis` satisfies this
 * structurally, and so does the in-memory fake the tests inject.
 */
export type ApiRedis = RedisProbe & PostStoreCommands;

export interface AppDeps {
  readonly config: ApiConfig;
  readonly logger: Logger;
  /** Redis, holder of the post records and the recent-posts index. */
  readonly redis: ApiRedis;
  /**
   * The queue client. The full interface is injected even though the API only
   * calls `enqueue` and `depth`, because the entrypoint owns one client and
   * closes it on shutdown (task 3.6).
   */
  readonly queue: QueueClient;
  /**
   * Custom metrics. Defaults to a fresh recorder with its own registry and no
   * Datadog sink, so `/metrics` answers even when nothing was injected — the
   * entrypoint passes one wired to the tracer's DogStatsD client when tracing is
   * on.
   */
  readonly metrics?: Metrics;
  /** Defaults to {@link INERT_TRACING}: no trace ids, no propagation headers. */
  readonly tracing?: Tracing;
  /** Per-dependency budget for the readiness checks. */
  readonly readinessTimeoutMs?: number;
  /** Injectable clock and id source, for deterministic tests. */
  readonly now?: (() => Date) | undefined;
  readonly generatePostId?: (() => string) | undefined;
}

export function createApp(deps: AppDeps): Express {
  const app = express();
  const metrics = deps.metrics ?? createMetrics({ config: deps.config });
  const tracing = deps.tracing ?? INERT_TRACING;

  // Advertising the framework tells an attacker which CVE list to start from.
  app.disable('x-powered-by');

  app.use(createRequestLogger(deps.logger));
  app.use(createRequestMetrics(metrics));

  app.use(createSecurityHeaders());
  app.use(createCors(deps.config));

  app.use(
    createMetricsRouter({
      metrics,
      backend: deps.config.queue.backend,
      queueDepth: () => deps.queue.depth(),
    }),
  );

  app.use(
    createHealthRouter({
      config: deps.config,
      redis: deps.redis,
      queue: deps.queue,
      ...(deps.readinessTimeoutMs === undefined
        ? {}
        : { readinessTimeoutMs: deps.readinessTimeoutMs }),
    }),
  );

  // The store is derived from the injected Redis rather than injected itself:
  // there is one implementation, and its keys are part of the contract with the
  // worker, so choosing them is not a caller's decision.
  const store = new RedisPostStore(deps.redis);

  app.use(
    createPublishRouter({
      store,
      queue: deps.queue,
      metrics,
      traceHeaders: () => tracing.traceHeaders(),
      now: deps.now,
      generatePostId: deps.generatePostId,
    }),
  );

  app.use(createQueryRouter({ store }));

  // Nothing matched, and then: nothing worked. Both answer in the same envelope
  // as every other failure, and both must stay last in that order.
  app.use(createNotFoundHandler());
  app.use(createErrorHandler());

  return app;
}
