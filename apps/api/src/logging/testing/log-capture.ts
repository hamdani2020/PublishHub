/**
 * Test destination that keeps parsed log lines in memory.
 *
 * Asserting on the parsed JSON rather than on a string is the point: the
 * requirement is about which fields a log line carries, not about how pino
 * happens to order them.
 */

import type { DestinationStream } from 'pino';

export interface LogCapture {
  readonly stream: DestinationStream;
  /** Every line written so far, newest last. */
  readonly lines: Record<string, unknown>[];
  /** Raw text, for asserting one object per line. */
  readonly raw: string[];
  /** Resolves once at least `count` lines have arrived, or throws on timeout. */
  waitFor(count: number, timeoutMs?: number): Promise<Record<string, unknown>[]>;
}

export function createLogCapture(): LogCapture {
  const lines: Record<string, unknown>[] = [];
  const raw: string[] = [];

  const stream: DestinationStream = {
    write(chunk: string): void {
      raw.push(chunk);
      lines.push(JSON.parse(chunk) as Record<string, unknown>);
    },
  };

  async function waitFor(count: number, timeoutMs = 2000): Promise<Record<string, unknown>[]> {
    const deadline = Date.now() + timeoutMs;
    while (lines.length < count) {
      if (Date.now() > deadline) {
        throw new Error(`timed out waiting for ${count} log line(s), saw ${lines.length}`);
      }
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    return lines;
  }

  return { stream, lines, raw, waitFor };
}
