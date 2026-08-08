/**
 * The only file in the queue abstraction that touches the AWS SDK. Keeping the
 * SDK behind `SqsPort` is what lets every backend test run against a fake with
 * no credentials, no region, and no network.
 *
 * Credentials come from the ambient AWS chain — IRSA in the cluster, the local
 * profile on a laptop. No access keys are read from configuration.
 */

import {
  DeleteMessageCommand,
  GetQueueAttributesCommand,
  ReceiveMessageCommand,
  SendMessageCommand,
  SQSClient,
} from '@aws-sdk/client-sqs';

import type { SqsMessage, SqsPort } from './sqs-queue-client.js';

export interface AwsSqsPortOptions {
  region: string;
}

export class AwsSqsPort implements SqsPort {
  private readonly client: SQSClient;

  constructor(options: AwsSqsPortOptions | SQSClient) {
    this.client = options instanceof SQSClient ? options : new SQSClient({ region: options.region });
  }

  async sendMessage(input: {
    queueUrl: string;
    messageBody: string;
    messageAttributes?: Record<string, string> | undefined;
  }): Promise<void> {
    const attributes = input.messageAttributes;
    await this.client.send(
      new SendMessageCommand({
        QueueUrl: input.queueUrl,
        MessageBody: input.messageBody,
        ...(attributes === undefined
          ? {}
          : {
              MessageAttributes: Object.fromEntries(
                Object.entries(attributes).map(([key, value]) => [
                  key,
                  { DataType: 'String', StringValue: value },
                ]),
              ),
            }),
      }),
    );
  }

  async receiveMessage(input: {
    queueUrl: string;
    waitTimeSeconds: number;
  }): Promise<SqsMessage[]> {
    const response = await this.client.send(
      new ReceiveMessageCommand({
        QueueUrl: input.queueUrl,
        MaxNumberOfMessages: 1,
        // Long polling: the worker waits on the queue instead of spinning
        // (Requirement 3.2).
        WaitTimeSeconds: input.waitTimeSeconds,
      }),
    );

    return (response.Messages ?? []).flatMap((message) => {
      if (
        message.MessageId === undefined ||
        message.ReceiptHandle === undefined ||
        message.Body === undefined
      ) {
        return [];
      }
      return [
        {
          messageId: message.MessageId,
          receiptHandle: message.ReceiptHandle,
          body: message.Body,
        },
      ];
    });
  }

  async deleteMessage(input: { queueUrl: string; receiptHandle: string }): Promise<void> {
    await this.client.send(
      new DeleteMessageCommand({
        QueueUrl: input.queueUrl,
        ReceiptHandle: input.receiptHandle,
      }),
    );
  }

  async approximateNumberOfMessages(queueUrl: string): Promise<number> {
    const response = await this.client.send(
      new GetQueueAttributesCommand({
        QueueUrl: queueUrl,
        AttributeNames: ['ApproximateNumberOfMessages'],
      }),
    );

    const raw = response.Attributes?.['ApproximateNumberOfMessages'];
    const depth = raw === undefined ? Number.NaN : Number.parseInt(raw, 10);
    return Number.isFinite(depth) ? depth : 0;
  }

  async close(): Promise<void> {
    this.client.destroy();
  }
}
