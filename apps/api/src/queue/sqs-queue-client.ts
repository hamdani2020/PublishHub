/**
 * SQS backend — the AWS path (Requirement 5.3).
 *
 * | Operation    | SQS API                                                     |
 * |--------------|-------------------------------------------------------------|
 * | `enqueue`    | `SendMessage`                                               |
 * | `receive`    | `ReceiveMessage` with long polling                          |
 * | `ack`        | `DeleteMessage`                                             |
 * | `deadLetter` | `SendMessage` to the DLQ + `DeleteMessage`, or the redrive policy |
 * | `depth`      | `GetQueueAttributes ApproximateNumberOfMessages`            |
 *
 * The message body is the same JSON text the Redis backend stores, and no
 * contract data is carried in message attributes, so a message captured from
 * one backend can be replayed into the other.
 */

import { describeJob, parsePublishJob, serializePublishJob } from './publish-job.js';
import type {
  DeadLetterEvent,
  JobHandle,
  PublishJob,
  QueueClient,
  ReceivedJob,
} from './types.js';

/** SQS caps `WaitTimeSeconds` at 20. */
export const SQS_MAX_WAIT_SECONDS = 20;

export interface SqsMessage {
  readonly messageId: string;
  readonly receiptHandle: string;
  readonly body: string;
}

/**
 * The narrow slice of SQS this client uses. `AwsSqsPort` in `aws-sqs-port.ts`
 * implements it over `@aws-sdk/client-sqs`; the unit tests implement it with a
 * fake, so no test needs AWS credentials.
 */
export interface SqsPort {
  sendMessage(input: {
    queueUrl: string;
    messageBody: string;
    messageAttributes?: Record<string, string> | undefined;
  }): Promise<void>;
  receiveMessage(input: { queueUrl: string; waitTimeSeconds: number }): Promise<SqsMessage[]>;
  deleteMessage(input: { queueUrl: string; receiptHandle: string }): Promise<void>;
  approximateNumberOfMessages(queueUrl: string): Promise<number>;
  close(): Promise<void>;
}

export interface SqsQueueClientOptions {
  queueUrl: string;
  /**
   * Optional. When set, `deadLetter` sends the message to this queue explicitly
   * and deletes it from the main queue. When unset, the message is left in place
   * for the queue's redrive policy to move after `maxReceiveCount` receives.
   */
  deadLetterQueueUrl?: string | undefined;
  onDeadLetter?: ((event: DeadLetterEvent) => void) | undefined;
}

export class SqsQueueClient implements QueueClient {
  private readonly sqs: SqsPort;
  private readonly queueUrl: string;
  private readonly deadLetterQueueUrl: string | null;
  private readonly onDeadLetter: (event: DeadLetterEvent) => void;

  constructor(sqs: SqsPort, options: SqsQueueClientOptions) {
    this.sqs = sqs;
    this.queueUrl = options.queueUrl;
    this.deadLetterQueueUrl = options.deadLetterQueueUrl ?? null;
    this.onDeadLetter = options.onDeadLetter ?? (() => {});
  }

  async enqueue(job: PublishJob): Promise<void> {
    await this.sqs.sendMessage({
      queueUrl: this.queueUrl,
      messageBody: serializePublishJob(job),
    });
  }

  async receive(waitSeconds: number): Promise<ReceivedJob | null> {
    const waitTimeSeconds = Math.min(
      SQS_MAX_WAIT_SECONDS,
      Math.max(0, Math.trunc(waitSeconds)),
    );

    const messages = await this.sqs.receiveMessage({
      queueUrl: this.queueUrl,
      waitTimeSeconds,
    });

    const message = messages[0];
    if (message === undefined) {
      return null;
    }

    const handle: JobHandle = {
      backend: 'sqs',
      messageId: message.messageId,
      receiptHandle: message.receiptHandle,
    };
    const parsed = parsePublishJob(message.body);

    return parsed.ok
      ? { raw: message.body, handle, job: parsed.job, invalidReason: null, invalidDetail: null }
      : {
          raw: message.body,
          handle,
          job: null,
          invalidReason: parsed.reason,
          invalidDetail: parsed.detail,
        };
  }

  async ack(job: ReceivedJob): Promise<void> {
    await this.sqs.deleteMessage({
      queueUrl: this.queueUrl,
      receiptHandle: this.receiptHandle(job),
    });
  }

  async deadLetter(job: ReceivedJob, reason: string): Promise<void> {
    const receiptHandle = this.receiptHandle(job);
    const { jobId, postId, attempt } = describeJob(job.job);

    if (this.deadLetterQueueUrl !== null) {
      // Body unchanged so the dead letter stays replayable; the reason rides
      // along as an attribute, which no consumer reads as contract data.
      await this.sqs.sendMessage({
        queueUrl: this.deadLetterQueueUrl,
        messageBody: job.raw,
        messageAttributes: { DeadLetterReason: reason },
      });
      await this.sqs.deleteMessage({ queueUrl: this.queueUrl, receiptHandle });
    }
    // Otherwise the message is deliberately left untouched: it becomes visible
    // again after the visibility timeout and the queue's redrive policy moves it
    // to the DLQ once `maxReceiveCount` is reached. Deleting it here would
    // discard it instead of dead-lettering it.

    this.onDeadLetter({
      backend: 'sqs',
      reason,
      jobId,
      postId,
      attempt,
      viaRedrivePolicy: this.deadLetterQueueUrl === null,
    });
  }

  async depth(): Promise<number> {
    return this.sqs.approximateNumberOfMessages(this.queueUrl);
  }

  async close(): Promise<void> {
    await this.sqs.close();
  }

  private receiptHandle(job: ReceivedJob): string {
    if (job.handle.backend !== 'sqs') {
      throw new TypeError(
        `SqsQueueClient received a "${job.handle.backend}" handle; handles are not portable across backends`,
      );
    }
    return job.handle.receiptHandle;
  }
}
