/**
 * In-memory `QueueClient` for tests that care about what the API sent to the
 * queue rather than how a backend encodes it.
 *
 * `enqueueError` is the interesting knob: setting it makes `enqueue` reject the
 * way an unreachable Redis or a denied SQS call would, which is how the publish
 * endpoint's `503 QUEUE_UNAVAILABLE` path and its post-record rollback get
 * exercised without breaking a real connection.
 *
 * `receive` always reports an empty queue. The API only ever produces; consuming
 * is the Python worker's job, and a fake that pretended otherwise would invite a
 * test to assert behavior this service does not have.
 */

import type { PublishJob, QueueClient, ReceivedJob } from '../types.js';

export class FakeQueueClient implements QueueClient {
  /** Every job accepted, in order. */
  readonly enqueued: PublishJob[] = [];
  readonly acked: ReceivedJob[] = [];
  readonly deadLettered: Array<{ job: ReceivedJob; reason: string }> = [];
  closed = false;
  /** When set, `enqueue` rejects with it instead of accepting the job. */
  enqueueError: Error | null = null;

  async enqueue(job: PublishJob): Promise<void> {
    if (this.enqueueError !== null) {
      throw this.enqueueError;
    }
    this.enqueued.push(job);
  }

  async receive(_waitSeconds: number): Promise<ReceivedJob | null> {
    return null;
  }

  async ack(job: ReceivedJob): Promise<void> {
    this.acked.push(job);
  }

  async deadLetter(job: ReceivedJob, reason: string): Promise<void> {
    this.deadLettered.push({ job, reason });
  }

  async depth(): Promise<number> {
    return this.enqueued.length;
  }

  async close(): Promise<void> {
    this.closed = true;
  }
}
