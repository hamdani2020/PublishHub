/**
 * Logger tests (Requirement 14.3).
 *
 * What has to hold: one JSON object per line, always carrying the service name
 * and environment, carrying trace identifiers when a tracer supplies them, and
 * a readable level.
 */

import { describe, expect, it } from 'vitest';

import { loadConfig } from '../../config/index.js';
import { createLogger } from '../logger.js';
import { createLogCapture } from './log-capture.js';

const PRODUCTION_ENV = {
  NODE_ENV: 'production',
  CORS_ORIGINS: 'https://app.example.com',
  DD_ENV: 'prod',
  DD_SERVICE: 'publishhub-api',
  DD_VERSION: '1.4.2',
};

describe('createLogger', () => {
  it('writes one JSON object per line with service, environment, and version', () => {
    const capture = createLogCapture();
    const logger = createLogger(loadConfig(PRODUCTION_ENV), { destination: capture.stream });

    logger.info({ post_id: 'post_01HZX3QK7M9V4TDR8N2C5EAB6F' }, 'post accepted');

    expect(capture.raw).toHaveLength(1);
    expect(capture.raw[0]?.endsWith('\n')).toBe(true);
    expect(capture.lines[0]).toMatchObject({
      level: 'info',
      service: 'publishhub-api',
      env: 'prod',
      version: '1.4.2',
      msg: 'post accepted',
      post_id: 'post_01HZX3QK7M9V4TDR8N2C5EAB6F',
    });
  });

  it('records the level as a label and the time as RFC 3339', () => {
    const capture = createLogCapture();
    const logger = createLogger(loadConfig(PRODUCTION_ENV), { destination: capture.stream });

    logger.error('queue unavailable');

    const line = capture.lines[0];
    expect(line?.level).toBe('error');
    expect(String(line?.time)).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
  });

  it('omits version when the build does not stamp one', () => {
    const capture = createLogCapture();
    const logger = createLogger(loadConfig({}), { destination: capture.stream });

    logger.info('starting');

    expect(capture.lines[0]).not.toHaveProperty('version');
    expect(capture.lines[0]).toMatchObject({ env: 'development' });
  });

  it('merges trace identifiers from the tracer into every line', () => {
    const capture = createLogCapture();
    const logger = createLogger(loadConfig(PRODUCTION_ENV), {
      destination: capture.stream,
      traceContext: () => ({ dd: { trace_id: '1234567890', span_id: '987654321' } }),
    });

    logger.info('job enqueued');

    expect(capture.lines[0]).toMatchObject({
      dd: { trace_id: '1234567890', span_id: '987654321' },
    });
  });

  it('emits no trace fields while the tracer reports no active span', () => {
    const capture = createLogCapture();
    const logger = createLogger(loadConfig({}), {
      destination: capture.stream,
      traceContext: () => undefined,
    });

    logger.info('no span here');

    expect(capture.lines[0]).not.toHaveProperty('dd');
  });

  it('honours the level derived from NODE_ENV', () => {
    const development = createLogCapture();
    createLogger(loadConfig({}), { destination: development.stream }).debug('verbose');
    expect(development.lines).toHaveLength(1);

    const production = createLogCapture();
    createLogger(loadConfig(PRODUCTION_ENV), { destination: production.stream }).debug('verbose');
    expect(production.lines).toHaveLength(0);
  });
});
