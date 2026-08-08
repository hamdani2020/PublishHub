/**
 * SQS backend unit tests against a fake port (Requirement 5.3).
 *
 * Same observable behavior as the Redis backend, different primitives: delete
 * instead of list removal, long polling instead of a blocking pop, and a
 * redrive policy as the default dead-letter path.
 */

import { beforeEach, describe, expect, it } from 'vitest';

import { createPublishJob, serializePublishJob } from '../publish-job.js';
import { SQS_MAX_WAIT_SECONDS, SqsQueueClient } from '../sqs-queue-client.js';
import { FakeSqsPort } from './fake-sqs-port.js';
import type { DeadLetterEvent, PublishJob } from '../types.js';

const QUEUE_URL = 'https://sqs.us-east-1.amazonaws.com/123456789012/publishhub-jobs';
const DLQ_URL = 'https://sqs.us-east-1.amazonaws.com/123456789012/publishhub-jobs-dlq';

function job(overrides: Partial<Parameters<typeof createPublishJob>[0]> = {}): PublishJob {
  return createPublishJob({
    post_id: 'post_01HZX3QK7M9V4TDR8N2C5EAB6F',
    content: 'hello',
    platforms: ['twitter'],
    ...overrides,
  });
}

describe('SqsQueueClient', () => {
  let port: FakeSqsPort;
  let deadLettered: DeadLetterEvent[];
  let client: SqsQueueClient;

  beforeEach(() => {
    port = new FakeSqsPort({ defaultQueueUrl: QUEUE_URL });
    deadLettered = [];
    client = new SqsQueueClient(port, {
      queueUrl: QUEUE_URL,
      onDeadLetter: (event) => deadLettered.push(event),
    });
  });

  it('sends the same JSON body the Redis backend would store', async () => {
    const enqueued = job();
    await client.enqueue(enqueued);

    expect(port.sent).toEqual([
      { queueUrl: QUEUE_URL, messageBody: serializePublishJob(enqueued) },
    ]);
  });

  it('long-polls with the requested wait and returns the parsed job', async () => {
    const enqueued = job();
    port.seed(serializePublishJob(enqueued));

    const received = await client.receive(20);

    expect(port.receiveCalls).toEqual([{ queueUrl: QUEUE_URL, waitTimeSeconds: 20 }]);
    expect(received?.job).toEqual(enqueued);
    expect(received?.handle).toEqual({
      backend: 'sqs',
      messageId: 'message-1',
      receiptHandle: 'receipt-1',
    });
  });

  it('clamps the wait to the SQS maximum and floors it at zero', async () => {
    await client.receive(120);
    await client.receive(-1);

    expect(port.receiveCalls.map((call) => call.waitTimeSeconds)).toEqual([
      SQS_MAX_WAIT_SECONDS,
      0,
    ]);
  });

  it('returns null when the long poll finds nothing', async () => {
    expect(await client.receive(1)).toBeNull();
  });

  it('deletes the message on ack', async () => {
    port.seed(serializePublishJob(job()));
    const received = await client.receive(20);

    await client.ack(received!);

    expect(port.deleted).toEqual([{ queueUrl: QUEUE_URL, receiptHandle: 'receipt-1' }]);
  });

  it('leaves the message for the redrive policy when no DLQ url is configured', async () => {
    port.seed(serializePublishJob(job()));
    const received = await client.receive(20);

    await client.deadLetter(received!, 'max_attempts_exhausted');

    // Deleting here would discard the message instead of dead-lettering it: the
    // queue's redrive policy is what moves it after maxReceiveCount.
    expect(port.deleted).toEqual([]);
    expect(port.sent).toEqual([]);
    expect(deadLettered[0]?.viaRedrivePolicy).toBe(true);
  });

  it('sends to the DLQ and deletes from the main queue when a DLQ url is configured', async () => {
    const explicit = new SqsQueueClient(port, {
      queueUrl: QUEUE_URL,
      deadLetterQueueUrl: DLQ_URL,
      onDeadLetter: (event) => deadLettered.push(event),
    });
    const enqueued = job();
    port.seed(serializePublishJob(enqueued));
    const received = await explicit.receive(20);

    await explicit.deadLetter(received!, 'schema_validation_failed');

    expect(port.sent).toEqual([
      {
        queueUrl: DLQ_URL,
        // Body unchanged, so the dead letter can be replayed into either backend.
        messageBody: serializePublishJob(enqueued),
        messageAttributes: { DeadLetterReason: 'schema_validation_failed' },
      },
    ]);
    expect(port.deleted).toEqual([{ queueUrl: QUEUE_URL, receiptHandle: 'receipt-1' }]);
    expect(deadLettered[0]).toEqual({
      backend: 'sqs',
      reason: 'schema_validation_failed',
      jobId: enqueued.job_id,
      postId: enqueued.post_id,
      attempt: 1,
      viaRedrivePolicy: false,
    });
  });

  it('surfaces an unparseable body instead of throwing', async () => {
    port.seed('["twitter", "linkedin"]');

    const received = await client.receive(20);

    expect(received?.job).toBeNull();
    expect(received?.invalidReason).toBe('unparseable_payload');
    expect(received?.raw).toBe('["twitter", "linkedin"]');
  });

  it('reports an unknown schema version as such', async () => {
    port.seed(JSON.stringify({ ...job(), schema_version: 2 }));

    expect((await client.receive(20))?.invalidReason).toBe('unknown_schema_version');
  });

  it('reports approximate queue depth, the number KEDA scales on', async () => {
    port.depthValue = 42;
    expect(await client.depth()).toBe(42);
  });

  it('rejects a handle from the other backend rather than deleting the wrong message', async () => {
    await expect(
      client.ack({
        raw: '{}',
        handle: { backend: 'redis', payload: '{}' },
        job: null,
        invalidReason: null,
        invalidDetail: null,
      }),
    ).rejects.toThrow(/not portable across backends/);
  });

  it('closes the underlying client', async () => {
    await client.close();
    expect(port.closed).toBe(true);
  });
});
