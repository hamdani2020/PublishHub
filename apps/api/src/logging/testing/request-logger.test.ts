/**
 * Request logging tests (Requirement 2.6).
 *
 * Driven through a real Express app on an ephemeral port rather than fake req and
 * res objects: response timing, aborted requests, and the error path all depend
 * on real socket events, and a fake would assert the mock instead of the
 * behavior.
 */

import type { AddressInfo } from 'node:net';

import express from 'express';
import type { ErrorRequestHandler, Express } from 'express';
import { afterEach, describe, expect, it } from 'vitest';

import { loadConfig } from '../../config/index.js';
import { createLogger } from '../logger.js';
import { CORRELATION_ID_HEADER, createRequestLogger, requestPath } from '../request-logger.js';
import { createLogCapture } from './log-capture.js';
import type { LogCapture } from './log-capture.js';

interface Harness {
  readonly capture: LogCapture;
  readonly url: string;
  close(): Promise<void>;
}

const harnesses: Harness[] = [];

afterEach(async () => {
  await Promise.all(harnesses.splice(0).map((harness) => harness.close()));
});

async function startApi(): Promise<Harness> {
  const capture = createLogCapture();
  const logger = createLogger(loadConfig({}), { destination: capture.stream });

  const app: Express = express();
  app.use(createRequestLogger(logger));
  app.get('/health', (_req, res) => {
    res.status(200).json({ status: 'ok' });
  });
  app.get('/api/v1/posts', (_req, res) => {
    res.status(200).json({ posts: [] });
  });
  app.get('/api/v1/posts/:id', (_req, res) => {
    res.status(404).json({ error: { code: 'NOT_FOUND' } });
  });
  app.get('/boom', () => {
    throw new Error('kaboom');
  });
  // Stands in for the error-handling middleware the API mounts: it hands the
  // real error to the response so the request log line carries it, while the
  // client only ever sees the generic envelope (Requirement 2.7).
  app.use(((error: Error, _req, res, _next) => {
    res.err = error;
    res.status(500).json({ error: { code: 'INTERNAL_ERROR' } });
  }) as ErrorRequestHandler);

  const server = app.listen(0);
  await new Promise<void>((resolve, reject) => {
    server.once('listening', resolve);
    server.once('error', reject);
  });
  const { port } = server.address() as AddressInfo;

  const harness: Harness = {
    capture,
    url: `http://127.0.0.1:${String(port)}`,
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

describe('createRequestLogger', () => {
  it('logs method, path, status code, duration, and correlation id for a handled request', async () => {
    const api = await startApi();

    const response = await fetch(`${api.url}/api/v1/posts?limit=20`);
    expect(response.status).toBe(200);

    const [line] = await api.capture.waitFor(1);
    expect(line).toMatchObject({
      level: 'info',
      method: 'GET',
      // The query string is dropped: it can carry user content.
      path: '/api/v1/posts',
      status_code: 200,
      msg: 'request completed',
      service: 'publishhub-api',
    });
    expect(typeof line?.duration_ms).toBe('number');
    expect(line?.duration_ms as number).toBeGreaterThanOrEqual(0);
    expect(String(line?.correlation_id)).not.toBe('');
  });

  it('emits exactly one flat line per request, with no nested req or res object', async () => {
    const api = await startApi();

    await fetch(`${api.url}/health`);
    const lines = await api.capture.waitFor(1);

    expect(lines).toHaveLength(1);
    expect(lines[0]).not.toHaveProperty('req');
    expect(lines[0]).not.toHaveProperty('res');
    expect(lines[0]).not.toHaveProperty('reqId');
    expect(lines[0]).not.toHaveProperty('responseTime');
  });

  it('reuses a caller-supplied correlation id and echoes it back', async () => {
    const api = await startApi();

    const response = await fetch(`${api.url}/health`, {
      headers: { [CORRELATION_ID_HEADER]: 'req-abc-123' },
    });

    expect(response.headers.get(CORRELATION_ID_HEADER)).toBe('req-abc-123');
    const [line] = await api.capture.waitFor(1);
    expect(line?.correlation_id).toBe('req-abc-123');
  });

  it('accepts x-correlation-id from proxies that use that name', async () => {
    const api = await startApi();

    await fetch(`${api.url}/health`, { headers: { 'x-correlation-id': 'edge-42' } });

    const [line] = await api.capture.waitFor(1);
    expect(line?.correlation_id).toBe('edge-42');
  });

  it('replaces an unusable inbound correlation id instead of logging it', async () => {
    const api = await startApi();

    const hostile = 'a'.repeat(200);
    const response = await fetch(`${api.url}/health`, {
      headers: { [CORRELATION_ID_HEADER]: hostile },
    });

    const [line] = await api.capture.waitFor(1);
    expect(line?.correlation_id).not.toBe(hostile);
    // A generated id is a v4 UUID, so it is safe to log and to echo.
    expect(String(line?.correlation_id)).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
    expect(response.headers.get(CORRELATION_ID_HEADER)).toBe(line?.correlation_id);
  });

  it('generates a distinct correlation id per request when none is supplied', async () => {
    const api = await startApi();

    await fetch(`${api.url}/health`);
    await fetch(`${api.url}/health`);
    const lines = await api.capture.waitFor(2);

    expect(lines[0]?.correlation_id).not.toBe(lines[1]?.correlation_id);
  });

  it('logs a client error at warn level', async () => {
    const api = await startApi();

    const response = await fetch(`${api.url}/api/v1/posts/unknown`);
    expect(response.status).toBe(404);

    const [line] = await api.capture.waitFor(1);
    expect(line).toMatchObject({ level: 'warn', status_code: 404, path: '/api/v1/posts/unknown' });
  });

  it('logs a server error at error level with the full error server-side', async () => {
    const api = await startApi();

    const response = await fetch(`${api.url}/boom`);
    expect(response.status).toBe(500);

    const [line] = await api.capture.waitFor(1);
    expect(line).toMatchObject({ level: 'error', status_code: 500, path: '/boom' });
    expect(line?.err).toMatchObject({ type: 'Error', message: 'kaboom' });
    expect(String((line?.err as { stack?: unknown }).stack)).toContain('request-logger.test.ts');
  });
});

describe('requestPath', () => {
  it('keeps the path and drops the query string', () => {
    expect(requestPath('/api/v1/posts')).toBe('/api/v1/posts');
    expect(requestPath('/api/v1/posts?limit=5&cursor=abc')).toBe('/api/v1/posts');
    expect(requestPath('/?q=1')).toBe('/');
  });

  it('falls back to / for a request with no url', () => {
    expect(requestPath(undefined)).toBe('/');
    expect(requestPath('')).toBe('/');
  });
});
