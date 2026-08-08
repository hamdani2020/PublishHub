/**
 * Central error handler and 404 handler (Requirement 2.7).
 *
 * The claim under test is a negative one — the client learns nothing — so the
 * assertions are written against the raw response text rather than the parsed
 * envelope. A leak does not have to appear in a field the test thought to check;
 * searching the whole body for the secret, the error class, and the word `at`
 * from a stack frame is what actually pins the requirement down.
 *
 * The thrown errors carry deliberately sensitive material: a Redis URL with a
 * password in it, and a stack. That is the realistic case — `ECONNREFUSED` from
 * `ioredis` really does carry the connection string — and it is exactly what must
 * end up in the log and nowhere else.
 */

import express from 'express';
import type { Express, Request, Response } from 'express';
import { afterEach, describe, expect, it } from 'vitest';

import { createApp } from '../../app.js';
import { loadConfig } from '../../config/index.js';
import { createLogger, createRequestLogger } from '../../logging/index.js';
import { createLogCapture } from '../../logging/testing/log-capture.js';
import type { LogCapture } from '../../logging/testing/log-capture.js';
import { FakeQueueClient } from '../../queue/testing/fake-queue-client.js';
import { FakeRedis } from '../../queue/testing/fake-redis.js';
import { INTERNAL_ERROR, NOT_FOUND } from '../errors.js';
import { INTERNAL_ERROR_MESSAGE, createErrorHandler, createNotFoundHandler } from '../error-handler.js';
import { listen } from './listen.js';
import type { RunningServer } from './listen.js';

/** A connection failure the way ioredis reports one: password and stack included. */
const LEAKY_ERROR_MESSAGE =
  'connect ECONNREFUSED 10.0.0.5:6379 (redis://default:sup3rs3cret@10.0.0.5:6379)';

interface ErrorBody {
  error: { code: string; message: string; request_id: string };
}

const running: RunningServer[] = [];

afterEach(async () => {
  await Promise.all(running.splice(0).map((server) => server.close()));
});

interface Harness {
  readonly url: string;
  readonly capture: LogCapture;
}

async function start(build: (capture: LogCapture) => Express): Promise<Harness> {
  const capture = createLogCapture();
  const server = await listen(build(capture));
  running.push(server);
  return { url: server.url, capture };
}

/**
 * The real request logger and the real error handler around routes that fail in
 * the ways a route can fail. `createApp` has no such route by design, so the
 * middleware pair is mounted here in the same order the app mounts it.
 */
function buildFailingApp(capture: LogCapture): Express {
  const config = loadConfig({});
  const app = express();
  app.disable('x-powered-by');
  app.use(createRequestLogger(createLogger(config, { destination: capture.stream })));

  app.get('/throws', () => {
    throw new Error(LEAKY_ERROR_MESSAGE);
  });

  app.get('/nexts', (_req: Request, _res: Response, next) => {
    next(new Error(LEAKY_ERROR_MESSAGE));
  });

  app.get('/throws-a-string', () => {
    // Not everything thrown in JavaScript is an Error, and the handler must not
    // assume otherwise. Throwing a bare string is the whole point of this route,
    // so the rule that forbids it is disabled here and nowhere else.
    // eslint-disable-next-line @typescript-eslint/only-throw-error
    throw 'sup3rs3cret raw string failure';
  });

  app.get('/throws-late', (_req: Request, res: Response) => {
    res.status(200);
    res.write('{"partial":');
    throw new Error(LEAKY_ERROR_MESSAGE);
  });

  app.use(createNotFoundHandler());
  app.use(createErrorHandler());
  return app;
}

/** The real application, for the paths that reach the handler through real routes. */
function buildRealApp(capture: LogCapture): Express {
  const config = loadConfig({});
  return createApp({
    config,
    logger: createLogger(config, { destination: capture.stream }),
    redis: new FakeRedis(),
    queue: new FakeQueueClient(),
  });
}

describe('central error handler', () => {
  for (const path of ['/throws', '/nexts']) {
    it(`answers 500 with the generic envelope for an error from ${path}`, async () => {
      const api = await start(buildFailingApp);

      const response = await fetch(`${api.url}${path}`);
      const raw = await response.text();
      const body = JSON.parse(raw) as ErrorBody;

      expect(response.status).toBe(500);
      expect(body.error.code).toBe(INTERNAL_ERROR);
      expect(body.error.message).toBe(INTERNAL_ERROR_MESSAGE);
      expect(body.error.request_id).toBe(response.headers.get('x-request-id'));
      expect(response.headers.get('cache-control')).toBe('no-store');
    });

    it(`leaks no error detail or stack frame in the ${path} response`, async () => {
      const api = await start(buildFailingApp);

      const raw = await (await fetch(`${api.url}${path}`)).text();

      expect(raw).not.toContain('sup3rs3cret');
      expect(raw).not.toContain('ECONNREFUSED');
      expect(raw).not.toContain('10.0.0.5');
      expect(raw).not.toContain('Error');
      expect(raw).not.toContain('stack');
      // A stack frame renders as `    at fn (file:line)`; the envelope has no
      // room for one, and this is the assertion that would catch it appearing.
      expect(raw).not.toMatch(/\bat\s+\S+\s+\(/);
      expect(raw).not.toContain(import.meta.url);
    });
  }

  it('logs the full error, with its stack, server-side', async () => {
    const api = await start(buildFailingApp);

    const response = await fetch(`${api.url}/throws`);

    const lines = await api.capture.waitFor(2);
    const failure = lines.find((line) => line.msg === 'unhandled error, responding 500');
    expect(failure).toMatchObject({ level: 'error' });
    const err = failure?.err as { message: string; stack: string; type: string };
    expect(err.message).toBe(LEAKY_ERROR_MESSAGE);
    expect(err.stack).toContain('at ');
    expect(failure?.correlation_id).toBe(response.headers.get('x-request-id'));
  });

  it('gives the request log line the real error rather than a synthetic one', async () => {
    // pino-http substitutes `failed with status code 500` unless the handler
    // assigns the error to `res.err`; the request line is where an operator looks
    // first, so it has to carry the actual failure.
    const api = await start(buildFailingApp);

    await fetch(`${api.url}/throws`);

    const lines = await api.capture.waitFor(2);
    const requestLine = lines.find((line) => line.status_code === 500);
    expect(requestLine).toMatchObject({ level: 'error', method: 'GET', path: '/throws' });
    expect((requestLine?.err as { message: string }).message).toBe(LEAKY_ERROR_MESSAGE);
  });

  it('answers 500 when the thrown value is not an Error', async () => {
    const api = await start(buildFailingApp);

    const response = await fetch(`${api.url}/throws-a-string`);
    const raw = await response.text();

    expect(response.status).toBe(500);
    expect((JSON.parse(raw) as ErrorBody).error.code).toBe(INTERNAL_ERROR);
    expect(raw).not.toContain('sup3rs3cret');
  });

  it('logs and cuts the connection when the failure arrives after the response started', async () => {
    const api = await start(buildFailingApp);

    // The status line is already sent, so there is no envelope to substitute. The
    // client must see a truncated response rather than a body that looks complete.
    let text: string | null;
    try {
      text = await (await fetch(`${api.url}/throws-late`)).text();
    } catch {
      // A truncated response surfaces as a network error, which is the correct
      // outcome: the client must not treat a partial body as complete.
      text = null;
    }
    expect(text).not.toBe('{"partial":}');
    expect(text ?? '').not.toContain('sup3rs3cret');

    const lines = await api.capture.waitFor(2);
    expect(
      lines.some((line) => line.msg === 'error thrown after the response had started'),
    ).toBe(true);
  });
});

describe('unmatched routes', () => {
  it('answers 404 in the standard envelope, not Express HTML', async () => {
    const api = await start(buildRealApp);

    const response = await fetch(`${api.url}/api/v1/nope`);
    const raw = await response.text();
    const body = JSON.parse(raw) as ErrorBody;

    expect(response.status).toBe(404);
    expect(body.error.code).toBe(NOT_FOUND);
    expect(body.error.request_id).toBe(response.headers.get('x-request-id'));
    expect(response.headers.get('content-type')).toContain('application/json');
    expect(raw).not.toContain('<html');
  });

  it('does not echo the requested path back to the client', async () => {
    const api = await start(buildRealApp);

    const raw = await (await fetch(`${api.url}/%3Cscript%3Ealert(1)%3C/script%3E`)).text();

    expect(raw).not.toContain('script');
  });

  it('answers 404 for a known path with the wrong method', async () => {
    const api = await start(buildRealApp);

    const response = await fetch(`${api.url}/api/v1/publish`, { method: 'DELETE' });

    expect(response.status).toBe(404);
    expect(((await response.json()) as ErrorBody).error.code).toBe(NOT_FOUND);
  });
});
