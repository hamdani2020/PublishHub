/**
 * In-memory stand-in for `SqsPort`, so SQS backend tests need no AWS account,
 * no credentials, and no network.
 *
 * Queues are keyed by url and a sent message is delivered to that queue, the way
 * a real SQS queue behaves. That is what lets a test enqueue and then receive
 * without reaching for internals.
 */

import type { SqsMessage, SqsPort } from '../sqs-queue-client.js';

export interface SentMessage {
  queueUrl: string;
  messageBody: string;
  messageAttributes?: Record<string, string> | undefined;
}

export interface DeletedMessage {
  queueUrl: string;
  receiptHandle: string;
}

export interface FakeSqsPortOptions {
  /** Queue used by `seed` when a test does not name one. */
  defaultQueueUrl?: string;
}

export class FakeSqsPort implements SqsPort {
  readonly queues = new Map<string, SqsMessage[]>();
  readonly sent: SentMessage[] = [];
  readonly deleted: DeletedMessage[] = [];
  readonly receiveCalls: Array<{ queueUrl: string; waitTimeSeconds: number }> = [];
  /** Value returned by `approximateNumberOfMessages`. */
  depthValue = 0;
  closed = false;

  private readonly defaultQueueUrl: string | undefined;
  private counter = 0;

  constructor(options: FakeSqsPortOptions = {}) {
    this.defaultQueueUrl = options.defaultQueueUrl;
  }

  private queue(queueUrl: string): SqsMessage[] {
    const existing = this.queues.get(queueUrl);
    if (existing !== undefined) {
      return existing;
    }
    const created: SqsMessage[] = [];
    this.queues.set(queueUrl, created);
    return created;
  }

  private nextHandles(): { messageId: string; receiptHandle: string } {
    this.counter += 1;
    return { messageId: `message-${this.counter}`, receiptHandle: `receipt-${this.counter}` };
  }

  /** Messages still waiting on a queue, oldest first. */
  messages(queueUrl: string | undefined = this.defaultQueueUrl): SqsMessage[] {
    return [...this.queue(this.resolveQueueUrl(queueUrl))];
  }

  /** Put a message on a queue as if a producer had already sent it. */
  seed(body: string, overrides: Partial<SqsMessage> & { queueUrl?: string } = {}): SqsMessage {
    const handles = this.nextHandles();
    const message: SqsMessage = {
      messageId: overrides.messageId ?? handles.messageId,
      receiptHandle: overrides.receiptHandle ?? handles.receiptHandle,
      body,
    };
    this.queue(this.resolveQueueUrl(overrides.queueUrl)).push(message);
    return message;
  }

  async sendMessage(input: {
    queueUrl: string;
    messageBody: string;
    messageAttributes?: Record<string, string> | undefined;
  }): Promise<void> {
    this.sent.push({ ...input });
    const handles = this.nextHandles();
    this.queue(input.queueUrl).push({ ...handles, body: input.messageBody });
  }

  async receiveMessage(input: {
    queueUrl: string;
    waitTimeSeconds: number;
  }): Promise<SqsMessage[]> {
    this.receiveCalls.push({ ...input });
    const message = this.queue(input.queueUrl).shift();
    return message === undefined ? [] : [message];
  }

  async deleteMessage(input: { queueUrl: string; receiptHandle: string }): Promise<void> {
    this.deleted.push({ ...input });
    const queue = this.queue(input.queueUrl);
    const index = queue.findIndex((message) => message.receiptHandle === input.receiptHandle);
    if (index >= 0) {
      queue.splice(index, 1);
    }
  }

  async approximateNumberOfMessages(_queueUrl: string): Promise<number> {
    return this.depthValue;
  }

  async close(): Promise<void> {
    this.closed = true;
  }

  private resolveQueueUrl(queueUrl: string | undefined): string {
    const resolved = queueUrl ?? this.defaultQueueUrl;
    if (resolved === undefined) {
      throw new Error(
        'FakeSqsPort needs a queue url: pass defaultQueueUrl to the constructor or name one per call',
      );
    }
    return resolved;
  }
}
