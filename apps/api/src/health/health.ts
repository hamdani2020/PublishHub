/**
 * Liveness and readiness endpoints (Requirements 2.3, 2.4).
 *
 * The distinction between the two is the whole point of this module, and it is
 * the design's recurring principle: **liveness reflects the process, readiness
 * reflects dependencies.** Conflating them is how a Redis blip becomes a cluster
 * of restarting API pods — the kubelet kills a process that was working fine and
 * would have recovered on its own.
 *
 * So:
 *
 * | Endpoint  | Touches Redis or the queue | Failure meaning                     |
 * |-----------|----------------------------|-------------------------------------|
 * | `/health` | Never                      | The process is wedged; restart it.   |
 * | `/ready`  | Every request              | Take this pod out of rotation.       |
 *
 * `/health` therefore stays 200 while Redis is unreachable at startup, while
 * `/ready` returns 503 until the dependency answers.
 *
 * Dependencies arrive as the two narrow probes below rather than as the full
 * `QueueClient` and `ioredis` instance. A readiness check needs `PING` and a
 * queue depth read and nothing else, and both real clients satisfy these shapes
 * structurally, so tests substitute a few lines of fake instead of a connection.
 */

import { Router } from 'express';
import type { Request, Response } from 'express';

import type { ApiConfig } from '../config/index.js';
import { DEPENDENCY_UNAVAILABLE, resolveRequestId } from '../http/index.js';
import type { QueueBackend } from '../queue/index.js';

/** Re-exported so existing importers of the health module keep working. */
export { DEPENDENCY_UNAVAILABLE };

/** Redis reachability. `ioredis`'s `ping()` satisfies this. */
export interface RedisProbe {
  ping(): Promise<unknown>;
}

/**
 * Queue reachability. `QueueClient.depth()` satisfies this: it is the cheapest
 * round trip that proves the backend is answering (`LLEN` on Redis,
 * `GetQueueAttributes` on SQS) and it is the same number KEDA scales on.
 */
export interface QueueProbe {
  depth(): Promise<number>;
}

/**
 * How long a single dependency check may take before it counts as unreachable.
 * A probe that hangs is operationally identical to one that fails, except that
 * hanging holds a socket open and produces no log line naming the culprit.
 */
export const DEFAULT_READINESS_TIMEOUT_MS = 2000;

export interface CheckResult {
  readonly status: 'ok' | 'error';
  readonly duration_ms: number;
}

export interface QueueCheckResult extends CheckResult {
  /** Which backend answered (or did not), for reading a 503 at a glance. */
  readonly backend: QueueBackend;
}

export interface LivenessBody {
  readonly status: 'ok';
  readonly service: string;
  readonly env: string;
  readonly version?: string;
  /** Process uptime in seconds. Rises monotonically; resets on restart. */
  readonly uptime_s: number;
  readonly pid: number;
}

export interface ReadinessBody {
  readonly status: 'ready' | 'unready';
  readonly checks: {
    readonly redis: CheckResult;
    readonly queue: QueueCheckResult;
  };
  /** Present only on 503, matching the error envelope used for every non-2xx. */
  readonly error?: {
    readonly code: typeof DEPENDENCY_UNAVAILABLE;
    readonly message: string;
    readonly request_id: string;
  };
}

export interface HealthRouterDeps {
  readonly config: ApiConfig;
  readonly redis: RedisProbe;
  readonly queue: QueueProbe;
  /** Defaults to {@link DEFAULT_READINESS_TIMEOUT_MS}. */
  readonly readinessTimeoutMs?: number;
  /** Overridable so a test can assert the reported uptime. */
  readonly uptimeSeconds?: () => number;
}

/** Raised when a dependency check outlives its budget. */
class ProbeTimeoutError extends Error {
  constructor(dependency: string, timeoutMs: number) {
    super(`${dependency} did not respond within ${String(timeoutMs)}ms`);
    this.name = 'ProbeTimeoutError';
  }
}

async function withTimeout<T>(
  operation: Promise<T>,
  dependency: string,
  timeoutMs: number,
): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      operation,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => {
          reject(new ProbeTimeoutError(dependency, timeoutMs));
        }, timeoutMs);
        // A pending readiness timer must never be the reason the process stays
        // alive during shutdown.
        timer.unref();
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

interface ProbeOutcome {
  readonly ok: boolean;
  readonly durationMs: number;
  readonly error: unknown;
}

/**
 * Run one dependency check. Never throws: a readiness probe reports failure, it
 * does not become one.
 */
async function runProbe(
  dependency: string,
  operation: () => Promise<unknown>,
  timeoutMs: number,
): Promise<ProbeOutcome> {
  const startedAt = performance.now();
  try {
    await withTimeout(operation(), dependency, timeoutMs);
    return { ok: true, durationMs: performance.now() - startedAt, error: null };
  } catch (error) {
    return { ok: false, durationMs: performance.now() - startedAt, error };
  }
}

function elapsed(durationMs: number): number {
  return Math.round(durationMs * 100) / 100;
}

export function createHealthRouter(deps: HealthRouterDeps): Router {
  const { config, redis, queue } = deps;
  const timeoutMs = deps.readinessTimeoutMs ?? DEFAULT_READINESS_TIMEOUT_MS;
  const uptimeSeconds = deps.uptimeSeconds ?? (() => process.uptime());
  const router = Router();

  router.get('/health', (_req: Request, res: Response<LivenessBody>) => {
    // No dependency call, no awaits: this answers even when everything
    // downstream is on fire, because "the process is running" is the only claim
    // it makes (Requirement 2.3).
    res.set('cache-control', 'no-store');
    res.status(200).json({
      status: 'ok',
      service: config.observability.service,
      env: config.observability.env,
      ...(config.observability.version === null ? {} : { version: config.observability.version }),
      uptime_s: elapsed(uptimeSeconds()),
      pid: process.pid,
    });
  });

  router.get('/ready', (req: Request, res: Response<ReadinessBody>) => {
    // Express 4 does not catch rejections from an async handler, and passing one
    // here would also trip `no-misused-promises`. `handleReady` resolves in all
    // cases, so voiding the promise is safe rather than merely convenient.
    void handleReady(req, res);
  });

  async function handleReady(req: Request, res: Response<ReadinessBody>): Promise<void> {
    // Both probes run concurrently: they are independent, and a serial pair
    // would make the worst case the sum of two timeouts (Requirement 2.4).
    const [redisOutcome, queueOutcome] = await Promise.all([
      runProbe('redis', () => redis.ping(), timeoutMs),
      runProbe('queue', () => queue.depth(), timeoutMs),
    ]);

    const failed: string[] = [];
    if (!redisOutcome.ok) {
      failed.push('redis');
      req.log.error({ dependency: 'redis', err: redisOutcome.error }, 'readiness check failed');
    }
    if (!queueOutcome.ok) {
      failed.push(`queue (${config.queue.backend})`);
      req.log.error(
        { dependency: 'queue', backend: config.queue.backend, err: queueOutcome.error },
        'readiness check failed',
      );
    }

    const checks = {
      redis: { status: redisOutcome.ok ? 'ok' : 'error', duration_ms: elapsed(redisOutcome.durationMs) },
      queue: {
        status: queueOutcome.ok ? 'ok' : 'error',
        duration_ms: elapsed(queueOutcome.durationMs),
        backend: config.queue.backend,
      },
    } as const satisfies ReadinessBody['checks'];

    res.set('cache-control', 'no-store');

    if (failed.length === 0) {
      res.status(200).json({ status: 'ready', checks });
      return;
    }

    // The body names the dependency, never the underlying error text: a
    // connection error can carry the Redis URL, credentials and all. The full
    // error went to the log above, correlated by the same request id.
    res.status(503).json({
      status: 'unready',
      checks,
      error: {
        code: DEPENDENCY_UNAVAILABLE,
        message: `not ready: ${failed.join(', ')} unreachable`,
        request_id: resolveRequestId(req),
      },
    });
  }

  return router;
}
