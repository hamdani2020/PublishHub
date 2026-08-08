/**
 * Process entrypoint. The only file in the service that reads `process.env`,
 * opens a socket, or installs a signal handler.
 *
 * Everything below it is injectable and unit-tested; this file is the wiring that
 * cannot be, so it is kept as thin as the job allows: load configuration, build
 * the logger, construct the two real clients, hand them to `createApp`, listen,
 * and register the shutdown sequence. The sequence itself lives in
 * `http/shutdown.ts` precisely so its ordering and its grace period can be
 * asserted without spawning a process.
 *
 * Startup is ordered so that the failures a deployment actually hits happen in
 * the most useful order. Configuration is validated first and exits non-zero with
 * the offending key (Requirement 5.5) — before that point there is no logger,
 * because the logger's fields come from the configuration. Redis and the queue
 * client are constructed lazily and never awaited here: a dependency that is not
 * up yet must show as `/ready` returning 503, not as a crash loop, which is
 * exactly the distinction `/health` and `/ready` exist to draw.
 *
 * Observability is initialized by the first import below and by nothing else.
 * `dd-trace` can only patch a module that has not been evaluated yet, and ES
 * modules evaluate their imports in order before the importing body runs, so the
 * tracer bootstrap has to be the first import in this file — above `ioredis`,
 * above `express`, above everything. Moving it changes behavior even though it
 * looks like an import-order cleanup. With `OBSERVABILITY_ENABLED` off it loads
 * nothing at all (Requirement 14.6).
 */

// KEEP FIRST. This import initializes the tracer as a side effect.
import { tracing } from './observability/bootstrap.js';

import { Redis } from 'ioredis';
import type { Logger } from 'pino';

import { createApp } from './app.js';
import { ConfigError, loadConfig } from './config/index.js';
import type { ApiConfig } from './config/index.js';
import { installShutdownHandlers } from './http/index.js';
import type { Shutdown, ShutdownResource } from './http/index.js';
import { createLogger } from './logging/index.js';
import { createMetrics } from './observability/index.js';
import { createQueueClientFromConfig } from './queue/index.js';

/**
 * Redis for the post records. The queue client builds its own connection when the
 * backend is Redis: a blocking `BRPOPLPUSH` occupies a connection for the length
 * of its timeout, so sharing one with the post store would stall reads.
 *
 * `lazyConnect` keeps construction from throwing, and `maxRetriesPerRequest: null`
 * keeps a command from failing while the client is still reconnecting.
 */
function createRedis(config: ApiConfig): Redis {
  return new Redis(config.redisUrl, { lazyConnect: true, maxRetriesPerRequest: null });
}

/**
 * `QUIT` when there is a connection to quit, `disconnect` when there is not.
 * `quit()` on a client that never connected waits for a connection that is not
 * coming, which would burn the grace period for no reason.
 */
async function closeRedis(redis: Redis): Promise<void> {
  if (redis.status === 'wait' || redis.status === 'end' || redis.status === 'close') {
    redis.disconnect();
    return;
  }
  await redis.quit();
}

function start(config: ApiConfig): void {
  // With tracing active every log line gains the ids of the span that produced it,
  // which is what makes a log and a trace one click apart (Requirement 14.3). With
  // it inert the provider returns undefined and the fields never appear, so the
  // logger's output is byte-identical to what it was before.
  const logger = createLogger(config, { traceContext: () => tracing.traceContext() });

  // The Prometheus registry is always live; the Datadog sink is the tracer's
  // DogStatsD client when tracing is on and a no-op recorder when it is not
  // (Requirement 14.6).
  const metrics = createMetrics({ config, sink: tracing.sink });

  const redis = createRedis(config);
  // Without a listener, ioredis emits `error` on the process and takes it down —
  // a transient blip would become a restart. Readiness reports the outage instead.
  redis.on('error', (error: Error) => {
    logger.error({ err: error }, 'redis connection error');
  });

  // No `onDeadLetter` hook: the API only ever produces. Consuming, and therefore
  // dead-lettering, is the worker's job.
  const queue = createQueueClientFromConfig(config.queue);

  const app = createApp({ config, logger, redis, queue, metrics, tracing });

  const server = app.listen(config.port, () => {
    logger.info(
      {
        port: config.port,
        node_env: config.nodeEnv,
        queue_backend: config.queue.backend,
        cors_origins: config.corsOrigins,
        // Logged because "why are there no traces" is a question with two answers,
        // and this line rules one of them out.
        tracing_enabled: tracing.enabled,
      },
      'api listening',
    );
  });

  // Reverse of construction order: the queue client first, then Redis.
  const resources: ShutdownResource[] = [
    { name: 'queue', close: () => queue.close() },
    { name: 'redis', close: () => closeRedis(redis) },
  ];

  const shutdown = installShutdownHandlers({ server, logger, resources });
  installFatalHandlers(logger, shutdown);
}

/**
 * A thrown error that escaped every handler, or a rejection nobody awaited, means
 * the process is in a state it was not designed for. Log it in full, then run the
 * same shutdown sequence and exit non-zero: draining first still gives the
 * in-flight requests their answer, and the non-zero code is what tells Kubernetes
 * this was not a clean stop.
 */
function installFatalHandlers(logger: Logger, shutdown: Shutdown): void {
  const fatal = (reason: string) => (error: unknown) => {
    logger.fatal({ err: error }, reason);
    void shutdown(reason).then(() => {
      process.exit(1);
    });
  };

  process.once('uncaughtException', fatal('uncaught exception'));
  process.once('unhandledRejection', fatal('unhandled rejection'));
}

function main(): void {
  let config: ApiConfig;
  try {
    config = loadConfig();
  } catch (error) {
    // No logger yet: its `service` and `env` fields come from the configuration
    // that just failed to load. stderr is the only honest channel here, and the
    // message names the offending variable.
    const detail = error instanceof ConfigError ? error.message : String(error);
    process.stderr.write(`api startup failed: ${detail}\n`);
    process.exitCode = 1;
    return;
  }
  start(config);
}

main();
