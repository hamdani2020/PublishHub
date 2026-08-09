import { describe, expect, it } from 'vitest';

import {
  DEFAULT_API_BASE_URL,
  FALLBACK_CONFIG,
  readRuntimeConfig,
  resolveRuntimeConfig,
} from '../runtime-config';

/**
 * Runtime configuration (Requirement 4.7).
 *
 * The value under test is written by a shell script into a browser global, so
 * the cases that matter are the malformed ones: every one of them has to produce
 * a working app on the documented default instead of an `undefined` in a URL.
 */
describe('resolveRuntimeConfig', () => {
  it('accepts a well-formed runtime value', () => {
    const resolution = resolveRuntimeConfig({ apiBaseUrl: '/api' });

    expect(resolution).toEqual({ config: { apiBaseUrl: '/api' }, source: 'runtime', problem: null });
  });

  it('accepts an absolute https base URL', () => {
    const resolution = resolveRuntimeConfig({ apiBaseUrl: 'https://api.example.com/gateway' });

    expect(resolution.source).toBe('runtime');
    expect(resolution.config.apiBaseUrl).toBe('https://api.example.com/gateway');
  });

  it('strips a trailing slash so callers can append a rooted path', () => {
    expect(resolveRuntimeConfig({ apiBaseUrl: '/api/' }).config.apiBaseUrl).toBe('/api');
    expect(resolveRuntimeConfig({ apiBaseUrl: 'https://api.example.com/' }).config.apiBaseUrl).toBe(
      'https://api.example.com',
    );
  });

  it('treats a bare same-origin root as an empty prefix', () => {
    expect(resolveRuntimeConfig({ apiBaseUrl: '/' }).config.apiBaseUrl).toBe('');
  });

  it('ignores surrounding whitespace', () => {
    expect(resolveRuntimeConfig({ apiBaseUrl: '  /api  ' }).config.apiBaseUrl).toBe('/api');
  });

  it('keeps unknown extra fields from failing the whole config', () => {
    const resolution = resolveRuntimeConfig({ apiBaseUrl: '/api', futureFlag: true });

    expect(resolution.source).toBe('runtime');
    expect(resolution.config).toEqual({ apiBaseUrl: '/api' });
  });

  describe('falls back to the documented default', () => {
    const cases: ReadonlyArray<readonly [name: string, candidate: unknown, problemIncludes: string]> = [
      ['the global is unset', undefined, 'is not set'],
      ['the global is null', null, 'is not set'],
      ['the global is a string', 'apiBaseUrl=/api', 'must be an object'],
      ['the global is an array', [{ apiBaseUrl: '/api' }], 'must be an object'],
      ['apiBaseUrl is absent', {}, 'apiBaseUrl is missing'],
      ['apiBaseUrl is blank', { apiBaseUrl: '   ' }, 'apiBaseUrl is missing'],
      ['apiBaseUrl is not a string', { apiBaseUrl: 8080 }, 'must be a string'],
      // The unsubstituted-placeholder case: an entrypoint that failed to expand
      // API_BASE_URL leaves a value that is neither a path nor a URL.
      ['apiBaseUrl is a bare host', { apiBaseUrl: 'api.example.com' }, 'must be an http(s) URL'],
      ['apiBaseUrl uses a non-http scheme', { apiBaseUrl: 'javascript:alert(1)' }, 'must be an http(s) URL'],
      ['apiBaseUrl is protocol-relative', { apiBaseUrl: '//api.example.com' }, 'must be an http(s) URL'],
      ['apiBaseUrl carries a query string', { apiBaseUrl: 'https://api.example.com/?v=1' }, 'must be an http(s) URL'],
    ];

    for (const [name, candidate, problemIncludes] of cases) {
      it(name, () => {
        const resolution = resolveRuntimeConfig(candidate);

        expect(resolution.source).toBe('fallback');
        expect(resolution.config).toEqual(FALLBACK_CONFIG);
        expect(resolution.config.apiBaseUrl).toBe(DEFAULT_API_BASE_URL);
        expect(resolution.problem).toContain(problemIncludes);
      });
    }
  });

  it('never throws, whatever it is handed', () => {
    const hostile = [Symbol('nope'), () => '/api', new Map([['apiBaseUrl', '/api']]), Number.NaN, true];

    for (const candidate of hostile) {
      expect(() => resolveRuntimeConfig(candidate)).not.toThrow();
      expect(resolveRuntimeConfig(candidate).config.apiBaseUrl).toBe(DEFAULT_API_BASE_URL);
    }
  });
});

describe('readRuntimeConfig', () => {
  it('reads the value from the injected global', () => {
    const resolution = readRuntimeConfig({ __PUBLISHHUB_CONFIG__: { apiBaseUrl: 'https://api.example.com' } });

    expect(resolution.source).toBe('runtime');
    expect(resolution.config.apiBaseUrl).toBe('https://api.example.com');
  });

  it('reads the real window when no argument is given', () => {
    window.__PUBLISHHUB_CONFIG__ = { apiBaseUrl: '/proxied-api' };
    try {
      expect(readRuntimeConfig().config.apiBaseUrl).toBe('/proxied-api');
    } finally {
      delete window.__PUBLISHHUB_CONFIG__;
    }
  });

  it('falls back when config.js never loaded', () => {
    delete window.__PUBLISHHUB_CONFIG__;

    const resolution = readRuntimeConfig();

    expect(resolution.source).toBe('fallback');
    expect(resolution.config.apiBaseUrl).toBe(DEFAULT_API_BASE_URL);
  });

  it('falls back when there is no window at all', () => {
    const resolution = readRuntimeConfig(undefined);

    expect(resolution.source).toBe('fallback');
    expect(resolution.config.apiBaseUrl).toBe(DEFAULT_API_BASE_URL);
  });
});
