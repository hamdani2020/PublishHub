/**
 * Startup configuration for the API service (Requirements 2.6, 2.9, 5.5, 14.3).
 *
 * Every variable from the design document's configuration reference that the API
 * reads is parsed and validated here, once, at startup. Nothing downstream
 * touches `process.env`: handlers receive an `ApiConfig` whose values are
 * already the right types and already known to be in range.
 *
 * Two rules shape this module:
 *
 * 1. Fail fast, and name the key. A bad value stops the process before it
 *    accepts traffic, with a message that says which variable is wrong
 *    (Requirement 5.5). A 503 an hour later from a malformed URL is a much
 *    worse failure mode than a refusal to boot.
 * 2. Do not re-derive what the queue layer already owns. Backend selection and
 *    its per-backend requirements live in `queue/factory.ts`; this module calls
 *    into it and rewraps its error so callers have a single type to catch.
 *
 * Worker-only variables (`MAX_ATTEMPTS`, `POLL_WAIT_SECONDS`,
 * `SIMULATE_LATENCY_MS`, `SIMULATE_FAILURE_RATE`) and the web-only
 * `API_BASE_URL` are deliberately absent: the API neither reads nor validates
 * configuration that belongs to another service.
 */

import { z } from 'zod';

import { DEFAULT_AWS_REGION, DEFAULT_REDIS_URL, QueueConfigError, resolveQueueConfig } from '../queue/index.js';
import type { Env, QueueConfig } from '../queue/index.js';
import { BOOLEAN_FLAG_HINT, parseBooleanFlag } from './flags.js';

/**
 * Thrown when a configuration value is missing or invalid. `key` names the
 * offending environment variable, and the message repeats it so a startup log
 * line is actionable on its own (Requirement 5.5).
 */
export class ConfigError extends Error {
  readonly key: string;

  constructor(key: string, message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = 'ConfigError';
    this.key = key;
  }
}

export const NODE_ENVS = ['development', 'test', 'production'] as const;
export type NodeEnv = (typeof NODE_ENVS)[number];

/** Log levels the service emits at. Not configurable: derived from `NODE_ENV`. */
export type LogLevel = 'debug' | 'info';

/** Local defaults, matching the configuration reference table in design.md. */
export const CONFIG_DEFAULTS = {
  PORT: '8080',
  NODE_ENV: 'development',
  CORS_ORIGINS: 'http://localhost:3000',
  REDIS_URL: DEFAULT_REDIS_URL,
  AWS_REGION: DEFAULT_AWS_REGION,
  OBSERVABILITY_ENABLED: 'false',
  DD_SERVICE: 'publishhub-api',
} as const;

export interface ObservabilityConfig {
  /** Master switch for tracing and metrics export. Off by default (14.6). */
  readonly enabled: boolean;
  /** `DD_SERVICE`. Also the `service` field on every log line (14.3). */
  readonly service: string;
  /** `DD_ENV`, falling back to `NODE_ENV`. The `env` field on every log line. */
  readonly env: string;
  /** `DD_VERSION`. Null when the build does not stamp one. */
  readonly version: string | null;
}

export interface ApiConfig {
  readonly port: number;
  readonly nodeEnv: NodeEnv;
  /**
   * Parsed `CORS_ORIGINS` allow-list. `['*']` is only reachable in development
   * (Requirement 2.9); anywhere else it is a startup failure.
   */
  readonly corsOrigins: readonly string[];
  /** True when the allow-list is exactly the development-only wildcard. */
  readonly allowAnyOrigin: boolean;
  /** Post records live in Redis regardless of which queue backend is active. */
  readonly redisUrl: string;
  readonly awsRegion: string;
  readonly queue: QueueConfig;
  readonly observability: ObservabilityConfig;
  readonly logLevel: LogLevel;
}

/** `us-east-1`, `eu-west-2`, `ap-southeast-1`, `us-gov-east-1`. */
const AWS_REGION_PATTERN = /^[a-z]{2}(-[a-z]+)+-\d$/;

// Accepted spellings live in `flags.ts` because the tracer bootstrap reads
// `OBSERVABILITY_ENABLED` before this module can be loaded, and the switch has to
// mean the same thing to both readers.
const booleanFlag = z
  .string()
  .refine((value) => parseBooleanFlag(value) !== null, BOOLEAN_FLAG_HINT)
  .transform((value) => parseBooleanFlag(value) === true);

const port = z
  .string()
  .regex(/^\d+$/, 'must be a positive integer')
  .transform(Number)
  .refine((value) => value >= 1 && value <= 65_535, 'must be between 1 and 65535');

const corsOrigins = z
  .string()
  .transform((value) =>
    value
      .split(',')
      .map((entry) => entry.trim())
      .filter((entry) => entry !== ''),
  )
  .refine((origins) => origins.length > 0, 'must list at least one origin, or the value *')
  .refine(
    (origins) => origins.every(isAllowedOrigin),
    'entries must be * or an http(s) origin such as https://app.example.com',
  );

const envSchema = z.object({
  PORT: port,
  NODE_ENV: z.enum(NODE_ENVS, {
    error: `must be one of ${NODE_ENVS.join(', ')}`,
  }),
  CORS_ORIGINS: corsOrigins,
  REDIS_URL: z.string(),
  AWS_REGION: z.string().regex(AWS_REGION_PATTERN, 'must be an AWS region such as us-east-1'),
  OBSERVABILITY_ENABLED: booleanFlag,
  DD_SERVICE: z.string(),
  DD_ENV: z.string().optional(),
  DD_VERSION: z.string().optional(),
});

function isAllowedOrigin(origin: string): boolean {
  if (origin === '*') {
    return true;
  }
  let parsed: URL;
  try {
    parsed = new URL(origin);
  } catch {
    return false;
  }
  // An origin is scheme + host + optional port. A path, query, or fragment means
  // the operator wrote a URL where an origin belongs, and the browser check
  // would silently never match.
  return (
    (parsed.protocol === 'http:' || parsed.protocol === 'https:') &&
    parsed.host !== '' &&
    parsed.pathname === '/' &&
    parsed.search === '' &&
    parsed.hash === '' &&
    origin === parsed.origin
  );
}

/**
 * Present-and-non-blank values only, with defaults filled in. Trimming and
 * treating `""` as unset matches the queue factory: an empty variable in a
 * container manifest means "not set", not "set to the empty string".
 */
function withDefaults(env: Env): Record<string, string> {
  const compacted: Record<string, string> = {};
  for (const [key, value] of Object.entries(env)) {
    if (typeof value !== 'string') {
      continue;
    }
    const trimmed = value.trim();
    if (trimmed !== '') {
      compacted[key] = trimmed;
    }
  }
  return { ...CONFIG_DEFAULTS, ...compacted };
}

function toConfigError(error: z.ZodError): ConfigError {
  const [issue] = error.issues;
  const key = issue?.path[0];
  const name = typeof key === 'string' ? key : 'configuration';
  return new ConfigError(name, `${name} ${issue?.message ?? 'is invalid'}`, { cause: error });
}

/**
 * Parse and validate the environment. Throws `ConfigError` on the first problem
 * found, naming the offending key.
 */
export function loadConfig(env: Env = process.env): ApiConfig {
  const parsed = envSchema.safeParse(withDefaults(env));
  if (!parsed.success) {
    throw toConfigError(parsed.error);
  }
  const values = parsed.data;

  if (values.CORS_ORIGINS.includes('*') && values.NODE_ENV !== 'development') {
    // Requirement 2.9: the wildcard is a local convenience, never a deployed
    // default. Rejecting it at startup is the only reliable place to catch it.
    throw new ConfigError(
      'CORS_ORIGINS',
      `CORS_ORIGINS may only contain * when NODE_ENV=development — NODE_ENV is ${values.NODE_ENV}`,
    );
  }

  // Redis holds the post records and the recent-posts index even when jobs go to
  // SQS, so its URL is validated unconditionally rather than per backend.
  const redisUrl = requireRedisUrl(values.REDIS_URL);

  let queue: QueueConfig;
  try {
    queue = resolveQueueConfig(env);
  } catch (error) {
    if (error instanceof QueueConfigError) {
      // Same failure, one error type for the caller to catch.
      throw new ConfigError(error.key, error.message, { cause: error });
    }
    throw error;
  }

  return {
    port: values.PORT,
    nodeEnv: values.NODE_ENV,
    corsOrigins: values.CORS_ORIGINS,
    allowAnyOrigin: values.CORS_ORIGINS.includes('*'),
    redisUrl,
    awsRegion: values.AWS_REGION,
    queue,
    observability: {
      enabled: values.OBSERVABILITY_ENABLED,
      service: values.DD_SERVICE,
      env: values.DD_ENV ?? values.NODE_ENV,
      version: values.DD_VERSION ?? null,
    },
    // Development gets debug detail; everywhere else stays at info so probe and
    // request lines do not bury the events that matter.
    logLevel: values.NODE_ENV === 'development' ? 'debug' : 'info',
  };
}

function requireRedisUrl(value: string): string {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new ConfigError('REDIS_URL', `REDIS_URL is not a valid URL: ${JSON.stringify(value)}`);
  }
  if (parsed.protocol !== 'redis:' && parsed.protocol !== 'rediss:') {
    throw new ConfigError(
      'REDIS_URL',
      `REDIS_URL must use redis: or rediss: — received ${JSON.stringify(parsed.protocol)}`,
    );
  }
  return value;
}
