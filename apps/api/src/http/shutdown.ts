/**
 * Graceful shutdown (Requirement 2.8).
 *
 * When Kubernetes scales down or rolls out a new version it sends `SIGTERM` and
 * starts a stopwatch. Whatever the process is doing when the grace period runs
 * out is killed. The sequence here exists so that what gets killed is nothing:
 *
 * 1. **Stop accepting connections.** `server.close()` refuses new sockets while
 *    leaving established ones to finish. Idle keep-alive sockets are swept away
 *    while it waits — they hold `close` open otherwise, and a browser or a proxy
 *    pool that has nothing more to send would keep the process hanging until the
 *    grace period ran out.
 * 2. **Drain in-flight requests.** `close` resolves once the last response has
 *    been written, so a publish that is mid-enqueue completes and answers.
 * 3. **Close dependencies, in reverse order of construction.** The queue client
 *    first, then Redis. Nothing is holding a handle by then, so the order is
 *    about being predictable rather than about correctness.
 * 4. **Exit.** Before the grace period, not because of it.
 *
 * Two properties make this testable, and are the reason it is a module rather
 * than a closure inside the entrypoint. It takes the server and the dependencies
 * as arguments, so a test can drive the whole sequence against fakes and assert
 * the order; and it returns an outcome instead of calling `process.exit` itself,
 * so the timeout path can be asserted without ending the test runner.
 *
 * A dependency that fails to close is logged and stepped over. The process is
 * going away regardless, and stopping the sequence on a failed `QUIT` would leave
 * the remaining connections open — the opposite of the point.
 */

import type { Logger } from 'pino';

/**
 * Total budget from signal to exit. Must stay comfortably under the chart's
 * `terminationGracePeriodSeconds` (task 9.2), so the process exits on its own
 * terms rather than being `SIGKILL`ed halfway through closing connections.
 */
export const DEFAULT_SHUTDOWN_GRACE_MS = 15_000;

/** Signals that mean "stop": Kubernetes sends the first, a terminal the second. */
export const SHUTDOWN_SIGNALS = ['SIGTERM', 'SIGINT'] as const;

/**
 * How often idle keep-alive sockets are swept while the listener drains.
 *
 * One sweep is not enough. A connection that is mid-request when shutdown starts
 * is not idle yet, and it becomes idle the moment its response is written — with
 * nothing to notice unless something looks again. Sweeping on a short interval
 * turns "waits for the client to give up" into "closes as soon as the last
 * response is out".
 */
export const IDLE_SWEEP_INTERVAL_MS = 50;

/**
 * What shutdown needs from an HTTP server. `http.Server` satisfies it;
 * `closeIdleConnections` and `closeAllConnections` are optional so a fake can
 * omit what it does not model.
 */
export interface DrainableServer {
  close(callback?: (error?: Error) => void): unknown;
  closeIdleConnections?: () => void;
  closeAllConnections?: () => void;
}

/** A connection to close once the listener has drained. */
export interface ShutdownResource {
  /** Named for the log line, e.g. `queue` or `redis`. */
  readonly name: string;
  close(): Promise<void> | void;
}

/** `drained` means the full sequence finished; `timed_out` means it did not. */
export type ShutdownOutcome = 'drained' | 'timed_out';

/** Runs the sequence. Safe to call more than once: later calls join the first. */
export type Shutdown = (reason: string) => Promise<ShutdownOutcome>;

export interface ShutdownDeps {
  readonly server: DrainableServer;
  readonly logger: Logger;
  /** Closed in this order, after the listener has drained. */
  readonly resources: readonly ShutdownResource[];
  /** Defaults to {@link DEFAULT_SHUTDOWN_GRACE_MS}. */
  readonly graceMs?: number;
}

function elapsedMs(startedAt: number): number {
  return Math.round(performance.now() - startedAt);
}

export function createShutdown(deps: ShutdownDeps): Shutdown {
  const { server, logger, resources } = deps;
  const graceMs = deps.graceMs ?? DEFAULT_SHUTDOWN_GRACE_MS;
  let running: Promise<ShutdownOutcome> | null = null;

  /**
   * Stop listening and wait for the in-flight responses.
   *
   * Never rejects: `close` reports "server not running" as an error, which during
   * shutdown is the desired state rather than a problem.
   */
  async function stopListening(): Promise<void> {
    let sweep: NodeJS.Timeout | undefined;
    try {
      await new Promise<void>((resolve) => {
        server.close((error) => {
          if (error === undefined || error === null) {
            logger.info('listener closed, in-flight requests drained');
          } else {
            logger.warn({ err: error }, 'listener was already closed');
          }
          resolve();
        });
        // Sockets that are idle right now, and then every socket as it goes idle.
        // A request still being served is untouched and gets to finish.
        server.closeIdleConnections?.();
        if (server.closeIdleConnections !== undefined) {
          sweep = setInterval(() => {
            server.closeIdleConnections?.();
          }, IDLE_SWEEP_INTERVAL_MS);
          sweep.unref();
        }
      });
    } finally {
      clearInterval(sweep);
    }
  }

  async function closeResource(resource: ShutdownResource): Promise<void> {
    const startedAt = performance.now();
    try {
      await resource.close();
      logger.info(
        { resource: resource.name, duration_ms: elapsedMs(startedAt) },
        'dependency closed',
      );
    } catch (error) {
      // Logged and swallowed: the remaining dependencies still have to be closed.
      logger.error(
        { resource: resource.name, err: error, duration_ms: elapsedMs(startedAt) },
        'failed to close dependency during shutdown',
      );
    }
  }

  async function drain(): Promise<ShutdownOutcome> {
    await stopListening();
    for (const resource of resources) {
      await closeResource(resource);
    }
    return 'drained';
  }

  async function run(reason: string): Promise<ShutdownOutcome> {
    const startedAt = performance.now();
    logger.info({ reason, grace_ms: graceMs }, 'shutdown started');

    let timer: NodeJS.Timeout | undefined;
    const expiry = new Promise<ShutdownOutcome>((resolve) => {
      timer = setTimeout(() => {
        resolve('timed_out');
      }, graceMs);
      // The grace timer must never be the thing keeping the process alive.
      timer.unref();
    });

    try {
      const outcome = await Promise.race([drain(), expiry]);
      if (outcome === 'drained') {
        logger.info({ reason, duration_ms: elapsedMs(startedAt) }, 'shutdown complete');
        return outcome;
      }
      // Out of time. Whatever is still open is cut off here rather than left for
      // SIGKILL, so the log records a deliberate give-up with a duration.
      logger.error(
        { reason, grace_ms: graceMs, duration_ms: elapsedMs(startedAt) },
        'shutdown grace period expired, forcing close',
      );
      server.closeAllConnections?.();
      return outcome;
    } finally {
      clearTimeout(timer);
    }
  }

  return function shutdown(reason: string): Promise<ShutdownOutcome> {
    if (running !== null) {
      // A rollout can send SIGTERM and a developer can hit Ctrl-C. Restarting the
      // sequence would close a dependency twice and reset the grace budget.
      logger.warn({ reason }, 'shutdown already in progress, signal ignored');
      return running;
    }
    running = run(reason);
    return running;
  };
}

/** Minimal view of `process` for signal registration, so tests can substitute it. */
export interface SignalTarget {
  once(signal: string, handler: () => void): unknown;
}

export interface InstallShutdownDeps extends ShutdownDeps {
  /** Defaults to `process.exit`. */
  readonly exit?: (code: number) => void;
  /** Defaults to {@link SHUTDOWN_SIGNALS}. */
  readonly signals?: readonly string[];
  /** Defaults to `process`. */
  readonly target?: SignalTarget;
}

/**
 * Register the signal handlers and return the shutdown function, so the
 * entrypoint can also trigger it from a fatal error.
 *
 * Exit code 1 on timeout: an incomplete shutdown is a real failure, and the
 * difference shows up in the pod's termination reason.
 */
export function installShutdownHandlers(deps: InstallShutdownDeps): Shutdown {
  const shutdown = createShutdown(deps);
  const exit = deps.exit ?? ((code: number) => process.exit(code));
  const target = deps.target ?? process;

  for (const signal of deps.signals ?? SHUTDOWN_SIGNALS) {
    // `once`, not `on`: a second signal of the same kind must not queue a second
    // sequence. `createShutdown` guards that too; this keeps the handler honest.
    target.once(signal, () => {
      void shutdown(signal).then((outcome) => {
        exit(outcome === 'drained' ? 0 : 1);
      });
    });
  }

  return shutdown;
}
