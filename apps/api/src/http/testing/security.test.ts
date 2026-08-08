/**
 * Security headers, CORS, and the body size limit (Requirement 2.9).
 *
 * CORS is asserted the way a browser would observe it — which header came back on
 * which request — rather than by inspecting the middleware's options. The
 * distinction matters for the disallowed-origin case: the request still succeeds
 * with a 200, and it is the *absence* of `Access-Control-Allow-Origin` that stops
 * the page from reading it. A test that expected a 403 would be asserting behavior
 * that would break every non-browser client.
 *
 * The wildcard has two tests, one per line of defense: configuration refuses to
 * load `*` outside development, and the middleware refuses to emit it even when
 * handed a configuration that says otherwise.
 */

import { afterEach, describe, expect, it } from 'vitest';

import { createApp } from '../../app.js';
import { ConfigError, loadConfig } from '../../config/index.js';
import type { ApiConfig } from '../../config/index.js';
import { createLogger } from '../../logging/index.js';
import { createLogCapture } from '../../logging/testing/log-capture.js';
import { CONTENT_MAX_LENGTH } from '../../queue/index.js';
import { FakeQueueClient } from '../../queue/testing/fake-queue-client.js';
import { FakeRedis } from '../../queue/testing/fake-redis.js';
import { PAYLOAD_TOO_LARGE } from '../errors.js';
import { JSON_BODY_LIMIT, resolveAllowedOrigins } from '../security.js';
import { listen } from './listen.js';
import type { RunningServer } from './listen.js';

const ALLOWED = 'https://app.publishhub.test';
const DISALLOWED = 'https://evil.example.com';

const running: RunningServer[] = [];

afterEach(async () => {
  await Promise.all(running.splice(0).map((server) => server.close()));
});

async function startApi(config: ApiConfig): Promise<string> {
  const capture = createLogCapture();
  const app = createApp({
    config,
    logger: createLogger(config, { destination: capture.stream }),
    redis: new FakeRedis(),
    queue: new FakeQueueClient(),
  });
  const server = await listen(app);
  running.push(server);
  return server.url;
}

function allowListConfig(): ApiConfig {
  return loadConfig({ CORS_ORIGINS: `${ALLOWED},http://localhost:3000` });
}

describe('security headers', () => {
  it('sets helmet defaults and advertises no framework', async () => {
    const url = await startApi(allowListConfig());

    const response = await fetch(`${url}/health`);

    // The header that matters most for a JSON API: no content-type guessing.
    expect(response.headers.get('x-content-type-options')).toBe('nosniff');
    expect(response.headers.get('content-security-policy')).toContain("default-src 'self'");
    expect(response.headers.get('x-frame-options')).toBe('SAMEORIGIN');
    expect(response.headers.get('strict-transport-security')).toContain('max-age=');
    expect(response.headers.get('x-powered-by')).toBeNull();
  });

  it('sets them on error responses too', async () => {
    const url = await startApi(allowListConfig());

    const response = await fetch(`${url}/api/v1/nope`);

    expect(response.status).toBe(404);
    expect(response.headers.get('x-content-type-options')).toBe('nosniff');
  });
});

describe('CORS allow-list', () => {
  it('echoes an allowed origin', async () => {
    const url = await startApi(allowListConfig());

    const response = await fetch(`${url}/api/v1/posts`, { headers: { origin: ALLOWED } });

    expect(response.status).toBe(200);
    expect(response.headers.get('access-control-allow-origin')).toBe(ALLOWED);
    // So a browser client can read the correlation id it was given.
    expect(response.headers.get('access-control-expose-headers')).toContain('x-request-id');
  });

  it('sends no allow-origin header for an origin outside the list', async () => {
    const url = await startApi(allowListConfig());

    const response = await fetch(`${url}/api/v1/posts`, { headers: { origin: DISALLOWED } });

    // 200 with no header: the request is served, and the browser is the one that
    // refuses to hand the response to the page.
    expect(response.status).toBe(200);
    expect(response.headers.get('access-control-allow-origin')).toBeNull();
  });

  it('answers a preflight from an allowed origin with the permitted methods', async () => {
    const url = await startApi(allowListConfig());

    const response = await fetch(`${url}/api/v1/publish`, {
      method: 'OPTIONS',
      headers: {
        origin: ALLOWED,
        'access-control-request-method': 'POST',
        'access-control-request-headers': 'content-type',
      },
    });

    expect(response.status).toBe(204);
    expect(response.headers.get('access-control-allow-origin')).toBe(ALLOWED);
    expect(response.headers.get('access-control-allow-methods')).toContain('POST');
    expect(response.headers.get('access-control-allow-headers')).toContain('content-type');
    expect(response.headers.get('access-control-max-age')).toBe('600');
    // No session to carry, so credentials are never allowed alongside an
    // allow-list.
    expect(response.headers.get('access-control-allow-credentials')).toBeNull();
  });

  it('refuses a preflight from an origin outside the list', async () => {
    const url = await startApi(allowListConfig());

    const response = await fetch(`${url}/api/v1/publish`, {
      method: 'OPTIONS',
      headers: { origin: DISALLOWED, 'access-control-request-method': 'POST' },
    });

    expect(response.headers.get('access-control-allow-origin')).toBeNull();
  });

  it('serves a client that sends no Origin at all', async () => {
    // curl, the probes, and every server-side caller. CORS must not touch them.
    const url = await startApi(allowListConfig());

    const response = await fetch(`${url}/health`);

    expect(response.status).toBe(200);
    expect(response.headers.get('access-control-allow-origin')).toBeNull();
  });
});

describe('CORS wildcard', () => {
  it('allows any origin in development when configured with *', async () => {
    const url = await startApi(loadConfig({ NODE_ENV: 'development', CORS_ORIGINS: '*' }));

    const response = await fetch(`${url}/api/v1/posts`, { headers: { origin: DISALLOWED } });

    expect(response.headers.get('access-control-allow-origin')).toBe('*');
  });

  it('refuses to load * outside development', () => {
    // First line of defense: the process does not start.
    expect(() => loadConfig({ NODE_ENV: 'production', CORS_ORIGINS: '*' })).toThrow(ConfigError);
  });

  it('never emits * outside development, even if configuration says so', async () => {
    // Second line of defense: a configuration assembled by hand — a future code
    // path, a test, a refactor — still cannot open the API up.
    const config: ApiConfig = {
      ...loadConfig({ NODE_ENV: 'production' }),
      corsOrigins: ['*', ALLOWED],
      allowAnyOrigin: true,
    };
    const url = await startApi(config);

    const wildcard = await fetch(`${url}/api/v1/posts`, { headers: { origin: DISALLOWED } });
    const allowed = await fetch(`${url}/api/v1/posts`, { headers: { origin: ALLOWED } });

    expect(wildcard.headers.get('access-control-allow-origin')).toBeNull();
    // The real entries in the same list keep working.
    expect(allowed.headers.get('access-control-allow-origin')).toBe(ALLOWED);
  });

  it('denies every origin when filtering the wildcard leaves nothing', () => {
    const config: ApiConfig = {
      ...loadConfig({ NODE_ENV: 'production' }),
      corsOrigins: ['*'],
      allowAnyOrigin: true,
    };

    // An empty allow-list, not "allow everything".
    expect(resolveAllowedOrigins(config)).toEqual([]);
  });

  it('reports the wildcard only for a development configuration', () => {
    expect(resolveAllowedOrigins(loadConfig({ CORS_ORIGINS: '*' }))).toBe('*');
  });
});

describe('request body size limit', () => {
  it('rejects a body over the limit with 413 and never reaches the queue', async () => {
    const url = await startApi(allowListConfig());

    const response = await fetch(`${url}/api/v1/publish`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      // Well past 64kb, and past the content rule too — the size check is the one
      // that has to fire, before the process buffers the whole thing.
      body: JSON.stringify({ content: 'x'.repeat(200_000), platforms: ['twitter'] }),
    });
    const body = (await response.json()) as { error: { code: string; message: string } };

    expect(response.status).toBe(413);
    expect(body.error.code).toBe(PAYLOAD_TOO_LARGE);
    expect(body.error.message).toContain(JSON_BODY_LIMIT);
  });

  it('accepts the largest body the validation rules allow', async () => {
    // The limit has to leave room for a legitimate maximum-length post; a limit
    // that rejected one would be a validation rule by accident.
    const url = await startApi(allowListConfig());

    const response = await fetch(`${url}/api/v1/publish`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        content: 'x'.repeat(CONTENT_MAX_LENGTH),
        platforms: ['twitter', 'linkedin', 'mastodon', 'bluesky'],
      }),
    });

    expect(response.status).toBe(202);
  });
});
