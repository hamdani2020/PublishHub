/**
 * `POST /api/v1/publish` (Requirements 2.1, 2.2).
 *
 * The whole endpoint is four steps in a fixed order:
 *
 * 1. Validate. A bad body is a `400 VALIDATION_FAILED` and **nothing is
 *    enqueued** — no id is minted, no record is written.
 * 2. Persist the post record, status `queued`.
 * 3. Enqueue the job envelope.
 * 4. Respond `202 { id, status: "queued" }`.
 *
 * Persist-then-enqueue, not the other way round: a worker can claim a job within
 * milliseconds, and it needs a record to update. The cost of that ordering is a
 * record that exists while the enqueue fails, so an enqueue failure compensates by
 * deleting the record before answering `503 QUEUE_UNAVAILABLE` — the design's
 * error-handling table requires no partial post record be left behind. The
 * failure is logged with the correlation id; the client learns the queue is
 * unavailable and not why.
 *
 * `202` rather than `201`: the post is accepted, not published. Publishing happens
 * in the worker, and the status the client gets back says exactly that.
 *
 * Two observability concerns hang off the same four steps. The submission counter
 * is recorded on every outcome — `queued`, `rejected`, `failed` — because a
 * dashboard that only counts successes cannot show a submission cliff. And the
 * envelope carries the active span's Datadog headers, which is what joins this
 * request and the worker's processing of the job into one trace (Requirement
 * 14.2). Both arrive injected and both default to nothing, so with observability
 * off the envelope carries `{}` and no counter has an opinion.
 *
 * Body parsing lives here rather than app-wide, because this is the only route
 * with a body. Failures the parser raises — malformed JSON, a payload over
 * `JSON_BODY_LIMIT` — are not caught here either: they are thrown before the
 * handler exists to catch them, and the central error handler maps them to `400`
 * and `413` respectively.
 */

import express, { Router } from 'express';
import type { Request, Response } from 'express';

import {
  DEPENDENCY_UNAVAILABLE,
  JSON_BODY_LIMIT,
  QUEUE_UNAVAILABLE,
  VALIDATION_FAILED,
  sendError,
} from '../http/index.js';
import type { Metrics } from '../observability/index.js';
import { createPublishJob, formatEnqueuedAt } from '../queue/index.js';
import type { PublishJob, QueueClient } from '../queue/index.js';
import { generatePostId } from './post-id.js';
import type { PostRecord, PostStore } from './post-store.js';
import { validatePublishRequest } from './publish-schema.js';

export const PUBLISH_PATH = '/api/v1/publish';

/** The accepted response. Deliberately just the two fields the client needs. */
export interface PublishAcceptedBody {
  readonly id: string;
  readonly status: 'queued';
}

export interface PublishRouterDeps {
  readonly store: PostStore;
  /** Only `enqueue` is used here: the API produces, the worker consumes. */
  readonly queue: Pick<QueueClient, 'enqueue'>;
  /**
   * Submission counters. Optional so a test can build the router without them;
   * `createApp` always supplies one.
   */
  readonly metrics?: Pick<Metrics, 'postSubmitted' | 'postRejected'> | undefined;
  /**
   * Datadog propagation headers for the active span, or `{}` when tracing is off.
   * They ride along in the envelope so the worker's span continues this request's
   * trace instead of starting its own (Requirement 14.2).
   */
  readonly traceHeaders?: (() => Record<string, string>) | undefined;
  /** Injectable clock, so a test can assert the persisted timestamps. */
  readonly now?: (() => Date) | undefined;
  /** Injectable id source, so a test can assert what was stored under what key. */
  readonly generatePostId?: (() => string) | undefined;
}

export function createPublishRouter(deps: PublishRouterDeps): Router {
  const { store, queue, metrics } = deps;
  const now = deps.now ?? (() => new Date());
  const newPostId = deps.generatePostId ?? (() => generatePostId());
  const traceHeaders = deps.traceHeaders ?? (() => ({}));
  const router = Router();

  // The only route in the service with a body, so it is the only place that
  // parses one. The limit is the shared constant rather than body-parser's
  // 100 KB default; a payload over it, or one that is not JSON at all, is thrown
  // by the parser and mapped to its envelope by the central error handler.
  router.post(PUBLISH_PATH, express.json({ limit: JSON_BODY_LIMIT }), (req: Request, res: Response) => {
    // Express 4 does not catch rejections from an async handler, so the handler
    // resolves in every case and the promise is explicitly voided.
    void handlePublish(req, res);
  });

  async function handlePublish(req: Request, res: Response): Promise<void> {
    const validation = validatePublishRequest(req.body);
    if (!validation.ok) {
      // Warn, not error: a rejected payload is a client mistake, not an incident.
      req.log.warn({ reason: validation.message }, 'publish request rejected');
      metrics?.postRejected('rejected');
      sendError(req, res, 400, VALIDATION_FAILED, validation.message);
      return;
    }

    const { content, platforms } = validation.value;
    const submittedAt = formatEnqueuedAt(now());
    const postId = newPostId();

    // Built before either write so a malformed envelope fails here, in one place,
    // rather than after a record has been persisted.
    const job: PublishJob = createPublishJob({
      post_id: postId,
      content,
      platforms,
      enqueued_at: submittedAt,
      trace_context: traceHeaders(),
    });

    const record: PostRecord = {
      id: postId,
      content,
      platforms,
      status: 'queued',
      job_id: job.job_id,
      created_at: submittedAt,
      updated_at: submittedAt,
    };

    try {
      await store.save(record);
    } catch (error) {
      req.log.error({ err: error, post_id: postId }, 'failed to persist post record');
      metrics?.postSubmitted(platforms, 'failed');
      sendError(
        req,
        res,
        503,
        DEPENDENCY_UNAVAILABLE,
        'post store unavailable, the post was not accepted',
      );
      return;
    }

    try {
      await queue.enqueue(job);
    } catch (error) {
      req.log.error(
        { err: error, post_id: postId, job_id: job.job_id },
        'failed to enqueue publish job',
      );
      await rollback(req, postId);
      metrics?.postSubmitted(platforms, 'failed');
      sendError(req, res, 503, QUEUE_UNAVAILABLE, 'queue unavailable, the post was not accepted');
      return;
    }

    req.log.info(
      { post_id: postId, job_id: job.job_id, platforms: record.platforms },
      'publish request queued',
    );
    metrics?.postSubmitted(platforms, 'queued');
    const body: PublishAcceptedBody = { id: postId, status: 'queued' };
    res.status(202).json(body);
  }

  /**
   * Undo step 2 after step 3 failed. A failing rollback is logged and swallowed:
   * the client's answer is the same either way, and the alternative is a 500 that
   * hides the real cause. What is left behind is a record stuck at `queued` with
   * no job, which the log line names.
   */
  async function rollback(req: Request, postId: string): Promise<void> {
    try {
      await store.remove(postId);
    } catch (error) {
      req.log.error(
        { err: error, post_id: postId },
        'failed to roll back post record after enqueue failure',
      );
    }
  }

  return router;
}
