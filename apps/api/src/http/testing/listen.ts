/**
 * Start an Express app on an ephemeral port and hand back its base URL.
 *
 * The HTTP-level tests in this module drive real requests through a real server
 * rather than a mocked `req`/`res` pair, because what is under test is HTTP
 * behavior: which headers a response carries, what a browser would be allowed to
 * read, whether a body was truncated, and whether a listener stops accepting
 * connections. None of that is observable from a fake response object.
 */

import type { Server } from 'node:http';
import type { AddressInfo } from 'node:net';

import type { Express } from 'express';

export interface RunningServer {
  readonly url: string;
  /** Kept separately: `server.address()` reports null once the listener closes. */
  readonly port: number;
  readonly server: Server;
  /** Idempotent, so a test that already shut the server down can still clean up. */
  close(): Promise<void>;
}

export async function listen(app: Express): Promise<RunningServer> {
  const server = app.listen(0);
  await new Promise<void>((resolve, reject) => {
    server.once('listening', resolve);
    server.once('error', reject);
  });
  const { port } = server.address() as AddressInfo;

  return {
    url: `http://127.0.0.1:${String(port)}`,
    port,
    server,
    close: () =>
      new Promise<void>((resolve) => {
        if (!server.listening) {
          resolve();
          return;
        }
        server.closeAllConnections();
        server.close(() => {
          resolve();
        });
      }),
  };
}
