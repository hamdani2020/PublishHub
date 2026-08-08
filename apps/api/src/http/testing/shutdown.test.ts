/**
 * Graceful shutdown (Requirement 2.8).
 *
 * Two kinds of test here, and both are needed.
 *
 * The ordering, idempotence, and grace-period cases run against a fake server, so
 * they can assert the exact sequence and drive the timeout without waiting out a
 * real one. The drain case runs against a real server with a real request in
 * flight, because "finish in-flight requests" is a property of Node's
 * `server.close`, and a fake that resolved on command would prove nothing about
 * it.
 *
 * The recorded order is the assertion that matters: a dependency closed before the
 * listener drained is a request that answers 500 during a rollout.
 */

import { createConnection } from 'node:net';

import { afterEach, describe, expect, it } from 'vitest';

import { createApp } from '../../app.js';
import { loadConfig } from '../../config/index.js';
import { createLogger } from '../../logging/index.js';
import { createLogCapture } from '../../logging/testing/log-capture.js';
import type { LogCapture } from '../../logging/testing/log-capture.js';
import { FakeQueueClient } from '../../queue/testing/fake-queue-client.js';
import { FakeRedis } from '../../queue/testing/fake-redis.js';
import { createShutdown, installShutdownHandlers } from '../shutdown.js';
import type { DrainableServer, ShutdownResource } from '../shutdown.js';
import { listen } from './listen.js';
import type { RunningServer } from './listen.js';

const running: RunningServer[] = [];

afterEach(async () => {
  await Promise.all(running.splice(0).map((server) => server.close()));
});

/**
 * A server whose `close` completes only when the test says so, which is how the
 * drain step gets held open long enough to prove nothing after it has run.
 */
class FakeServer implements DrainableServer {
  closeIdleCalls = 0;
  closeAllCalls = 0;
  private pending: ((error?: Error) => void) | null = null;

  constructor(
    private readonly mode: 'immediate' | 'manual' | 'never' = 'immediate',
    private readonly closeError?: Error,
  ) {}

  close(callback?: (error?: Error) => void): this {
    if (this.mode === 'never') {
      return this;
    }
    if (this.mode === 'manual') {
      this.pending = callback ?? null;
      return this;
    }
    callback?.(this.closeError);
    return this;
  }

  /** Complete a `manual` close, as a drained in-flight request would. */
  finishClose(): void {
    const callback = this.pending;
    this.pending = null;
    callback?.();
  }

  closeIdleConnections = (): void => {
    this.closeIdleCalls += 1;
  };

  closeAllConnections = (): void => {
    this.closeAllCalls += 1;
  };
}

function recorder(): { order: string[]; resource: (name: string, error?: Error) => ShutdownResource } {
  const order: string[] = [];
  return {
    order,
    resource: (name, error) => ({
      name,
      close: async () => {
        order.push(name);
        if (error !== undefined) {
          throw error;
        }
      },
    }),
  };
}

function testLogger(capture: LogCapture) {
  return createLogger(loadConfig({}), { destination: capture.stream });
}

/** Whether a TCP connection to the port is accepted. False means the listener is gone. */
async function connects(port: number): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    const socket = createConnection({ host: '127.0.0.1', port });
    const settle = (accepted: boolean) => {
      socket.destroy();
      resolve(accepted);
    };
    socket.setTimeout(500);
    socket.once('connect', () => settle(true));
    socket.once('error', () => settle(false));
    socket.once('timeout', () => settle(false));
  });
}

describe('shutdown sequence', () => {
  it('stops the listener before closing any dependency, in the order given', async () => {
    const capture = createLogCapture();
    const server = new FakeServer('manual');
    const { order, resource } = recorder();
    const shutdown = createShutdown({
      server,
      logger: testLogger(capture),
      resources: [resource('queue'), resource('redis')],
    });

    const finished = shutdown('SIGTERM');
    // The listener has been asked to close and has not finished draining, so
    // nothing downstream may have been touched yet.
    expect(order).toEqual([]);
    expect(server.closeIdleCalls).toBe(1);

    server.finishClose();
    await expect(finished).resolves.toBe('drained');
    expect(order).toEqual(['queue', 'redis']);
  });

  it('logs each step of the sequence', async () => {
    const capture = createLogCapture();
    const { resource } = recorder();

    await createShutdown({
      server: new FakeServer(),
      logger: testLogger(capture),
      resources: [resource('queue'), resource('redis')],
    })('SIGTERM');

    const messages = capture.lines.map((line) => line.msg);
    expect(messages).toEqual([
      'shutdown started',
      'listener closed, in-flight requests drained',
      'dependency closed',
      'dependency closed',
      'shutdown complete',
    ]);
    expect(capture.lines[0]).toMatchObject({ reason: 'SIGTERM' });
    expect(capture.lines[2]).toMatchObject({ resource: 'queue' });
    expect(capture.lines[3]).toMatchObject({ resource: 'redis' });
  });

  it('closes the remaining dependencies when one fails to close', async () => {
    const capture = createLogCapture();
    const { order, resource } = recorder();

    const outcome = await createShutdown({
      server: new FakeServer(),
      logger: testLogger(capture),
      resources: [resource('queue', new Error('QUIT failed: connection already gone')), resource('redis')],
    })('SIGTERM');

    // A failed close must not strand the connections behind it.
    expect(outcome).toBe('drained');
    expect(order).toEqual(['queue', 'redis']);
    const failure = capture.lines.find(
      (line) => line.msg === 'failed to close dependency during shutdown',
    );
    expect(failure).toMatchObject({ level: 'error', resource: 'queue' });
  });

  it('completes even when the listener reports it was already closed', async () => {
    const capture = createLogCapture();
    const { order, resource } = recorder();

    const outcome = await createShutdown({
      server: new FakeServer('immediate', new Error('Server is not running.')),
      logger: testLogger(capture),
      resources: [resource('queue')],
    })('SIGTERM');

    expect(outcome).toBe('drained');
    expect(order).toEqual(['queue']);
  });

  it('runs once no matter how many signals arrive', async () => {
    const capture = createLogCapture();
    const server = new FakeServer('manual');
    const { order, resource } = recorder();
    const shutdown = createShutdown({
      server,
      logger: testLogger(capture),
      resources: [resource('queue'), resource('redis')],
    });

    const first = shutdown('SIGTERM');
    const second = shutdown('SIGINT');
    server.finishClose();

    await expect(first).resolves.toBe('drained');
    await expect(second).resolves.toBe('drained');
    // Closed once each: a second sequence would double-close and reset the budget.
    expect(order).toEqual(['queue', 'redis']);
    expect(
      capture.lines.some((line) => line.msg === 'shutdown already in progress, signal ignored'),
    ).toBe(true);
  });
});

describe('grace period', () => {
  it('gives up and forces the connections closed when the budget expires', async () => {
    const capture = createLogCapture();
    // A listener that never drains: a client holding a connection open forever.
    const server = new FakeServer('never');
    const { order, resource } = recorder();

    const outcome = await createShutdown({
      server,
      logger: testLogger(capture),
      resources: [resource('queue'), resource('redis')],
      graceMs: 25,
    })('SIGTERM');

    expect(outcome).toBe('timed_out');
    // Still stuck on the listener, so the dependencies were never reached.
    expect(order).toEqual([]);
    expect(server.closeAllCalls).toBe(1);
    const expiry = capture.lines.find(
      (line) => line.msg === 'shutdown grace period expired, forcing close',
    );
    expect(expiry).toMatchObject({ level: 'error', grace_ms: 25 });
  });

  it('reports drained when the sequence finishes inside the budget', async () => {
    const capture = createLogCapture();
    const server = new FakeServer('manual');
    const { resource } = recorder();
    const shutdown = createShutdown({
      server,
      logger: testLogger(capture),
      resources: [resource('queue')],
      graceMs: 2000,
    });

    const finished = shutdown('SIGTERM');
    server.finishClose();

    await expect(finished).resolves.toBe('drained');
    expect(server.closeAllCalls).toBe(0);
  });
});

describe('shutdown against a live server', () => {
  it('finishes an in-flight request, then closes the dependencies', async () => {
    const capture = createLogCapture();
    const config = loadConfig({});
    const redis = new FakeRedis();
    const queue = new FakeQueueClient();
    // A readiness probe that takes 150ms: long enough that the shutdown starts
    // while the request is still open.
    redis.ping = async () => {
      await new Promise((resolve) => setTimeout(resolve, 150));
      return 'PONG' as const;
    };

    const app = createApp({
      config,
      logger: createLogger(config, { destination: capture.stream }),
      redis,
      queue,
    });
    const server = await listen(app);
    running.push(server);

    const inFlight = fetch(`${server.url}/ready`);
    // Give the request time to reach the handler before the listener closes.
    await new Promise((resolve) => setTimeout(resolve, 20));

    const shutdown = createShutdown({
      server: server.server,
      logger: createLogger(config, { destination: capture.stream }),
      resources: [
        { name: 'queue', close: () => queue.close() },
        {
          name: 'redis',
          close: async () => {
            await redis.quit();
          },
        },
      ],
      graceMs: 5000,
    });
    const startedAt = Date.now();
    const outcome = await shutdown('SIGTERM');
    const drainMs = Date.now() - startedAt;

    // The request that was already accepted got its answer.
    const response = await inFlight;
    expect(response.status).toBe(200);
    expect(outcome).toBe('drained');
    // And the keep-alive socket that request left behind was swept as soon as it
    // went idle, instead of holding the drain open until the client lost interest.
    // Without the sweep this takes seconds, and in production it would spend the
    // whole grace period.
    expect(drainMs).toBeLessThan(1500);
    expect(queue.closed).toBe(true);
    expect(redis.closed).toBe(true);
    // And the listener is gone, so nothing new is accepted. Asserted with a bare
    // TCP connection rather than a `fetch`: the refusal happens at the socket, and
    // an HTTP client would spend seconds in its connection pool getting there.
    expect(server.server.listening).toBe(false);
    await expect(connects(server.port)).resolves.toBe(false);
  });
});

describe('signal handlers', () => {
  it('runs the sequence on SIGTERM and exits zero', async () => {
    const capture = createLogCapture();
    const handlers = new Map<string, () => void>();
    const exits: number[] = [];
    const { order, resource } = recorder();

    installShutdownHandlers({
      server: new FakeServer(),
      logger: testLogger(capture),
      resources: [resource('queue'), resource('redis')],
      exit: (code) => exits.push(code),
      target: { once: (signal, handler) => handlers.set(signal, handler) },
    });

    expect([...handlers.keys()]).toEqual(['SIGTERM', 'SIGINT']);

    handlers.get('SIGTERM')?.();
    // Let the sequence and the exit callback settle.
    await new Promise((resolve) => setTimeout(resolve, 10));

    expect(order).toEqual(['queue', 'redis']);
    expect(exits).toEqual([0]);
  });

  it('exits non-zero when the grace period expires', async () => {
    const capture = createLogCapture();
    const handlers = new Map<string, () => void>();
    const exits: number[] = [];

    installShutdownHandlers({
      server: new FakeServer('never'),
      logger: testLogger(capture),
      resources: [],
      graceMs: 25,
      exit: (code) => exits.push(code),
      target: { once: (signal, handler) => handlers.set(signal, handler) },
    });

    handlers.get('SIGTERM')?.();
    await new Promise((resolve) => setTimeout(resolve, 80));

    // An incomplete shutdown is a failure, and the exit code says so.
    expect(exits).toEqual([1]);
  });
});
