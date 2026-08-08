/**
 * Request and error counters (Requirement 14.4, and the 5xx-rate monitor in
 * design section 13).
 *
 * Counting happens on `finish`, when the status code is final, so a request that
 * ends in the error handler is counted with the 500 it actually returned rather
 * than with whatever the handler set before throwing. An aborted request never
 * finishes and is never counted, which is correct: it has no status code.
 *
 * Only the method and the status code become labels. The path does not: `/api/v1/
 * posts/:id` would put one series per post id into the registry, and a metrics
 * endpoint that grows with traffic is an outage waiting for a busy afternoon.
 * `/metrics` itself is skipped, so a scrape does not count as traffic and inflate
 * the denominator of the error-rate monitor.
 */

import type { NextFunction, Request, Response } from 'express';

import type { Metrics } from './metrics.js';
import { METRICS_PATH } from './metrics-router.js';

export function createRequestMetrics(metrics: Metrics) {
  return function recordRequest(req: Request, res: Response, next: NextFunction): void {
    if (req.path === METRICS_PATH) {
      next();
      return;
    }

    res.on('finish', () => {
      metrics.requestObserved({ method: req.method, statusCode: res.statusCode });
    });

    next();
  };
}
