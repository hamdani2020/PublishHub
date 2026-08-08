/**
 * `GET /metrics` — Prometheus exposition (design section 12's endpoint table).
 *
 * Served by the API itself rather than by a Datadog agent, so the numbers are
 * readable with `curl` on a laptop and by a Prometheus scrape in a cluster, with
 * or without an APM account (Requirement 14.6).
 *
 * Queue depth is sampled here, at scrape time, instead of being tracked on every
 * enqueue. Depth is a property of the queue, not of this process: two API replicas
 * both incrementing a local gauge would each report a fraction of the truth,
 * while one `LLEN` per scrape reports the same number KEDA scales on. A backend
 * that fails to answer leaves the previous value in place and does not fail the
 * scrape — a monitoring endpoint that goes down with its dependencies is the one
 * that cannot tell you why.
 *
 * The endpoint is unauthenticated, which is the convention for a Prometheus
 * target and is safe only because of what it exposes: request counts, submission
 * counts by platform, and queue depth. No post content, no ids, no configuration.
 * In the cluster it is reachable on the pod network, not through the ingress.
 */

import { Router } from 'express';
import type { Request, Response } from 'express';

import type { QueueBackend } from '../queue/index.js';
import type { Metrics } from './metrics.js';

export const METRICS_PATH = '/metrics';

export interface MetricsRouterDeps {
  readonly metrics: Metrics;
  /** Names the series; the same backend the readiness probe reports. */
  readonly backend: QueueBackend;
  /**
   * Reads pending depth. Omitted when there is nothing to ask — the gauge is then
   * simply absent from the output rather than reported as zero, which would be a
   * lie an alert could fire on.
   */
  readonly queueDepth?: (() => Promise<number>) | undefined;
}

export function createMetricsRouter(deps: MetricsRouterDeps): Router {
  const { metrics, backend, queueDepth } = deps;
  const router = Router();

  router.get(METRICS_PATH, (req: Request, res: Response) => {
    // Express 4 does not catch rejections from an async handler; `render` resolves
    // in every case, so voiding the promise is safe.
    void render(req, res);
  });

  async function render(req: Request, res: Response): Promise<void> {
    if (queueDepth !== undefined) {
      try {
        metrics.queueDepthObserved(backend, await queueDepth());
      } catch (error) {
        // Debug, not error: a scrape during a queue outage is expected, and the
        // readiness probe is already reporting the outage at error level.
        req.log.debug({ err: error, backend }, 'queue depth unavailable for metrics scrape');
      }
    }

    const { contentType, body } = await metrics.render();
    res.set('content-type', contentType);
    res.set('cache-control', 'no-store');
    res.status(200).send(body);
  }

  return router;
}
