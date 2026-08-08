/**
 * Custom metrics for the API (Requirements 14.4, 14.6).
 *
 * Two destinations, one call site. Every recording increments a Prometheus
 * counter in this process's registry — which is what `/metrics` serves and what
 * makes the numbers readable with no Datadog account at all — and is forwarded to
 * a {@link MetricsSink}. With `OBSERVABILITY_ENABLED=false` that sink is
 * {@link INERT_SINK}, so the Datadog side of the pair disappears and the
 * Prometheus side keeps working (design section 13).
 *
 * The design names the metrics in Datadog's dotted style
 * (`publishhub.posts.submitted`). Prometheus does not allow dots in a metric
 * name, so the same metric is exported with underscores and, for a monotonic
 * count, the conventional `_total` suffix. {@link METRICS} is the single mapping
 * between the two, so neither name is spelled out anywhere else.
 *
 * The API only records what the API can observe: posts submitted, queue depth,
 * and its own requests and errors. `publishhub.jobs.processed`,
 * `publishhub.jobs.failed`, and `publishhub.jobs.duration` describe work this
 * service never does — they belong to the worker, which emits them from the
 * process that actually ran the job.
 *
 * Every metric carries `env`, so a dashboard can separate staging from production
 * without a second data source. Label sets are otherwise kept small on purpose: a
 * counter labelled with a post id is a cardinality incident, not a metric.
 */

import { Counter, Gauge, Registry } from 'prom-client';

import type { ApiConfig } from '../config/index.js';
import type { Platform, QueueBackend } from '../queue/index.js';

/**
 * Datadog metric names from the design, paired with the Prometheus name the same
 * metric is exported under.
 */
export const METRICS = {
  postsSubmitted: {
    datadog: 'publishhub.posts.submitted',
    prometheus: 'publishhub_posts_submitted_total',
  },
  queueDepth: {
    datadog: 'publishhub.queue.depth',
    prometheus: 'publishhub_queue_depth',
  },
  apiRequests: {
    datadog: 'publishhub.api.requests',
    prometheus: 'publishhub_api_requests_total',
  },
  apiErrors: {
    datadog: 'publishhub.api.errors',
    prometheus: 'publishhub_api_errors_total',
  },
} as const;

/**
 * The Datadog side of a recording. `dd-trace` ships a DogStatsD client that
 * satisfies this shape; the inert implementation below satisfies it too, which is
 * how the observability switch stays a one-line decision at startup instead of a
 * conditional at every call site.
 */
export interface MetricsSink {
  increment(name: string, value: number, tags: Record<string, string>): void;
  gauge(name: string, value: number, tags: Record<string, string>): void;
}

/** Discards everything. Used whenever `OBSERVABILITY_ENABLED` is false. */
export const INERT_SINK: MetricsSink = {
  increment: () => undefined,
  gauge: () => undefined,
};

/** Status label on `publishhub.posts.submitted`. */
export type SubmissionStatus = 'queued' | 'rejected' | 'failed';

export interface Metrics {
  /** The registry `/metrics` renders. One per app, never the global default. */
  readonly registry: Registry;
  /**
   * One increment per requested platform, because "posts submitted" is only
   * useful broken down by where they were going.
   */
  postSubmitted(platforms: readonly Platform[], status: SubmissionStatus): void;
  /** A submission that never reached a platform list: no platform label to give. */
  postRejected(status: Exclude<SubmissionStatus, 'queued'>): void;
  /**
   * One call per answered request. A `5xx` also lands on the error counter, so
   * the monitor for "5xx rate above 1%" is a ratio of two series rather than a
   * query that has to know which status codes count as failures.
   */
  requestObserved(fields: { method: string; statusCode: number }): void;
  /** Sampled at scrape time rather than tracked incrementally. */
  queueDepthObserved(backend: QueueBackend, depth: number): void;
  /** Prometheus exposition text plus the content type that describes it. */
  render(): Promise<{ contentType: string; body: string }>;
}

export interface MetricsDeps {
  readonly config: ApiConfig;
  /**
   * Defaults to {@link INERT_SINK}. The entrypoint passes the DogStatsD client
   * from an initialized tracer; nothing else ever does, which is why no test in
   * this package loads `dd-trace`.
   */
  readonly sink?: MetricsSink;
  /**
   * Defaults to a fresh registry. A dedicated registry rather than prom-client's
   * global default keeps two apps in one test process from colliding over an
   * already-registered metric name.
   */
  readonly registry?: Registry;
}

export function createMetrics(deps: MetricsDeps): Metrics {
  const sink = deps.sink ?? INERT_SINK;
  const registry = deps.registry ?? new Registry();
  const env = deps.config.observability.env;

  const postsSubmitted = new Counter({
    name: METRICS.postsSubmitted.prometheus,
    help: 'Publish requests accepted, counted once per requested platform.',
    labelNames: ['platform', 'status', 'env'] as const,
    registers: [registry],
  });

  const queueDepth = new Gauge({
    name: METRICS.queueDepth.prometheus,
    help: 'Pending jobs on the queue, sampled when this endpoint is scraped.',
    labelNames: ['backend', 'env'] as const,
    registers: [registry],
  });

  const apiRequests = new Counter({
    name: METRICS.apiRequests.prometheus,
    help: 'HTTP requests answered by the API.',
    labelNames: ['method', 'status_code', 'env'] as const,
    registers: [registry],
  });

  const apiErrors = new Counter({
    name: METRICS.apiErrors.prometheus,
    help: 'HTTP requests answered with a server error (5xx).',
    labelNames: ['method', 'status_code', 'env'] as const,
    registers: [registry],
  });

  return {
    registry,

    postSubmitted(platforms, status) {
      for (const platform of platforms) {
        const tags = { platform, status, env };
        postsSubmitted.inc(tags, 1);
        sink.increment(METRICS.postsSubmitted.datadog, 1, tags);
      }
    },

    postRejected(status) {
      // `platform` is not optional in the label set: a series that sometimes has
      // the label and sometimes does not is awkward to aggregate, so a rejected
      // submission reports `none` rather than nothing.
      const tags = { platform: 'none', status, env };
      postsSubmitted.inc(tags, 1);
      sink.increment(METRICS.postsSubmitted.datadog, 1, tags);
    },

    requestObserved({ method, statusCode }) {
      const tags = { method, status_code: String(statusCode), env };
      apiRequests.inc(tags, 1);
      sink.increment(METRICS.apiRequests.datadog, 1, tags);
      if (statusCode >= 500) {
        apiErrors.inc(tags, 1);
        sink.increment(METRICS.apiErrors.datadog, 1, tags);
      }
    },

    queueDepthObserved(backend, depth) {
      const tags = { backend, env };
      queueDepth.set(tags, depth);
      sink.gauge(METRICS.queueDepth.datadog, depth, tags);
    },

    async render() {
      return { contentType: registry.contentType, body: await registry.metrics() };
    },
  };
}
