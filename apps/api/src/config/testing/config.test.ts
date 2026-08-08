/**
 * Configuration tests (Requirements 2.9, 5.5, 14.3).
 *
 * The behavior under test is not "zod works": it is that a valid environment
 * produces typed values with the documented defaults, and that every invalid
 * value stops startup with the name of the key that is wrong.
 */

import { describe, expect, it } from 'vitest';

import { DEFAULT_REDIS_URL } from '../../queue/index.js';
import { CONFIG_DEFAULTS, ConfigError, loadConfig } from '../config.js';;

const QUEUE_URL = 'https://sqs.us-east-1.amazonaws.com/123456789012/publishhub-jobs';

function expectConfigError(env: Record<string, string | undefined>, key: string): ConfigError {
  let thrown: unknown;
  try {
    loadConfig(env);
  } catch (error) {
    thrown = error;
  }

  expect(thrown, `expected loadConfig to reject ${key}`).toBeInstanceOf(ConfigError);
  const error = thrown as ConfigError;
  expect(error.key).toBe(key);
  // The message names the key, so a startup log line is actionable on its own.
  expect(error.message).toContain(key);
  return error;
}

describe('loadConfig defaults', () => {
  it('boots on an empty environment using the documented local defaults', () => {
    expect(loadConfig({})).toEqual({
      port: 8080,
      nodeEnv: 'development',
      corsOrigins: ['http://localhost:3000'],
      allowAnyOrigin: false,
      redisUrl: DEFAULT_REDIS_URL,
      awsRegion: 'us-east-1',
      queue: { backend: 'redis', redisUrl: DEFAULT_REDIS_URL },
      observability: {
        enabled: false,
        service: 'publishhub-api',
        env: 'development',
        version: null,
      },
      logLevel: 'debug',
    });
  });

  it('treats a blank variable as unset rather than as an error', () => {
    const config = loadConfig({ PORT: '   ', CORS_ORIGINS: '', DD_VERSION: ' ' });
    expect(config.port).toBe(Number(CONFIG_DEFAULTS.PORT));
    expect(config.corsOrigins).toEqual(['http://localhost:3000']);
    expect(config.observability.version).toBeNull();
  });

  it('stays at info level outside development', () => {
    expect(loadConfig({ NODE_ENV: 'production', CORS_ORIGINS: 'https://app.example.com' }).logLevel).toBe('info');
    expect(loadConfig({ NODE_ENV: 'test' }).logLevel).toBe('info');
  });
});

describe('loadConfig valid values', () => {
  it('parses a full production-shaped environment', () => {
    const config = loadConfig({
      PORT: '3000',
      NODE_ENV: 'production',
      CORS_ORIGINS: 'https://app.example.com, https://admin.example.com',
      QUEUE_BACKEND: 'sqs',
      SQS_QUEUE_URL: QUEUE_URL,
      AWS_REGION: 'eu-west-1',
      REDIS_URL: 'rediss://cache.example:6380',
      OBSERVABILITY_ENABLED: 'true',
      DD_SERVICE: 'publishhub-api',
      DD_ENV: 'prod',
      DD_VERSION: '1.4.2',
    });

    expect(config.port).toBe(3000);
    expect(config.nodeEnv).toBe('production');
    expect(config.corsOrigins).toEqual(['https://app.example.com', 'https://admin.example.com']);
    expect(config.allowAnyOrigin).toBe(false);
    expect(config.awsRegion).toBe('eu-west-1');
    // Redis is still configured with the SQS backend: post records live there.
    expect(config.redisUrl).toBe('rediss://cache.example:6380');
    expect(config.queue).toEqual({
      backend: 'sqs',
      queueUrl: QUEUE_URL,
      deadLetterQueueUrl: null,
      region: 'eu-west-1',
    });
    expect(config.observability).toEqual({
      enabled: true,
      service: 'publishhub-api',
      env: 'prod',
      version: '1.4.2',
    });
  });

  it('accepts every documented spelling of a boolean flag', () => {
    for (const value of ['true', 'TRUE', '1', 'yes', 'on']) {
      expect(loadConfig({ OBSERVABILITY_ENABLED: value }).observability.enabled, value).toBe(true);
    }
    for (const value of ['false', 'FALSE', '0', 'no', 'off']) {
      expect(loadConfig({ OBSERVABILITY_ENABLED: value }).observability.enabled, value).toBe(false);
    }
  });

  it('falls back to NODE_ENV for the log environment when DD_ENV is unset', () => {
    expect(loadConfig({ NODE_ENV: 'test' }).observability.env).toBe('test');
  });

  it('allows the CORS wildcard only in development', () => {
    const config = loadConfig({ NODE_ENV: 'development', CORS_ORIGINS: '*' });
    expect(config.corsOrigins).toEqual(['*']);
    expect(config.allowAnyOrigin).toBe(true);
  });

  it('accepts port boundary values', () => {
    expect(loadConfig({ PORT: '1' }).port).toBe(1);
    expect(loadConfig({ PORT: '65535' }).port).toBe(65_535);
  });
});

describe('loadConfig failures', () => {
  it('names PORT when it is not a usable port number', () => {
    expectConfigError({ PORT: 'eighty-eighty' }, 'PORT');
    expectConfigError({ PORT: '0' }, 'PORT');
    expectConfigError({ PORT: '70000' }, 'PORT');
    expectConfigError({ PORT: '8080.5' }, 'PORT');
    expectConfigError({ PORT: '-1' }, 'PORT');
  });

  it('names NODE_ENV when the value is not a known environment', () => {
    const error = expectConfigError({ NODE_ENV: 'staging' }, 'NODE_ENV');
    expect(error.message).toContain('development, test, production');
  });

  it('names CORS_ORIGINS when an entry is not an origin', () => {
    expectConfigError({ CORS_ORIGINS: 'app.example.com' }, 'CORS_ORIGINS');
    expectConfigError({ CORS_ORIGINS: 'https://app.example.com/callback' }, 'CORS_ORIGINS');
    expectConfigError({ CORS_ORIGINS: 'ftp://files.example.com' }, 'CORS_ORIGINS');
    expectConfigError({ CORS_ORIGINS: ',,' }, 'CORS_ORIGINS');
  });

  it('names CORS_ORIGINS when the wildcard is used outside development', () => {
    const error = expectConfigError({ NODE_ENV: 'production', CORS_ORIGINS: '*' }, 'CORS_ORIGINS');
    expect(error.message).toContain('NODE_ENV');
    expectConfigError(
      { NODE_ENV: 'production', CORS_ORIGINS: 'https://app.example.com,*' },
      'CORS_ORIGINS',
    );
  });

  it('names OBSERVABILITY_ENABLED when the flag is not a boolean', () => {
    expectConfigError({ OBSERVABILITY_ENABLED: 'maybe' }, 'OBSERVABILITY_ENABLED');
  });

  it('names AWS_REGION when it is not region-shaped', () => {
    expectConfigError({ AWS_REGION: 'useast1' }, 'AWS_REGION');
    expectConfigError({ AWS_REGION: 'US-EAST-1' }, 'AWS_REGION');
  });

  it('names REDIS_URL when the url is malformed or the wrong scheme', () => {
    expectConfigError({ REDIS_URL: 'not-a-url' }, 'REDIS_URL');
    expectConfigError({ REDIS_URL: 'http://localhost:6379' }, 'REDIS_URL');
  });

  it('reports the queue layer failure as a ConfigError naming the same key', () => {
    // Requirement 5.5: a missing per-backend setting fails at startup, and the
    // caller has one error type to catch regardless of which layer found it.
    expectConfigError({ QUEUE_BACKEND: 'sqs' }, 'SQS_QUEUE_URL');
    expectConfigError({ QUEUE_BACKEND: 'kafka' }, 'QUEUE_BACKEND');
    expectConfigError(
      { QUEUE_BACKEND: 'sqs', SQS_QUEUE_URL: QUEUE_URL, SQS_DLQ_URL: 'nope' },
      'SQS_DLQ_URL',
    );
  });

  it('does not read process.env when an environment is passed explicitly', () => {
    expect(() => loadConfig({ QUEUE_BACKEND: 'sqs' })).toThrow(ConfigError);
  });
});
