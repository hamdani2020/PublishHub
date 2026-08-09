import { describe, expect, it, vi } from 'vitest';

import type { ComposerDraft } from '../../composer';
import { PUBLISH_PATH, publishUrl, submitPost } from '../publish-client';

/**
 * The publish call and its outcome mapping (Requirements 4.2, 4.3).
 *
 * `fetch` is the only thing doubled here, and only because a unit test has no
 * API to talk to; the real `Response` type carries the status, body, and parse
 * behaviour, so the mapping is exercised against genuine responses rather than
 * an object shaped like one.
 *
 * Two invariants run through the whole file: an outcome is always returned
 * (never thrown), and an infrastructure message from the server never reaches
 * the user verbatim.
 */

const DRAFT: ComposerDraft = { content: 'Shipping the platform today.', platforms: ['twitter', 'bluesky'] };

function jsonResponse(status: number, body: unknown, url = `/api${PUBLISH_PATH}`): Response {
  const response = new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
  // `Response.url` is empty for a constructed response and read-only, so the
  // 404 message's "which URL did we actually call" path needs it defined here.
  Object.defineProperty(response, 'url', { value: url });
  return response;
}

function errorEnvelope(code: string, message: string, requestId = 'req_0f3c'): unknown {
  return { error: { code, message, request_id: requestId } };
}

function fetchReturning(response: Response): typeof fetch {
  return vi.fn<typeof fetch>().mockResolvedValue(response);
}

describe('submitPost request', () => {
  it('posts the draft as JSON to the versioned publish path', async () => {
    const fetchImpl = fetchReturning(jsonResponse(202, { id: 'post_abc123', status: 'queued' }));

    await submitPost(DRAFT, { apiBaseUrl: '/api', fetchImpl });

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = vi.mocked(fetchImpl).mock.calls[0]!;
    expect(url).toBe('/api/v1/publish');
    expect(init?.method).toBe('POST');
    expect(new Headers(init?.headers).get('content-type')).toBe('application/json');
    // The body is always a JSON string here; `BodyInit` is the wider type.
    expect(JSON.parse(init?.body as string)).toEqual({
      content: 'Shipping the platform today.',
      platforms: ['twitter', 'bluesky'],
    });
  });

  it('builds a same-origin URL when the base URL is empty', async () => {
    const fetchImpl = fetchReturning(jsonResponse(202, { id: 'post_abc123', status: 'queued' }));

    await submitPost(DRAFT, { apiBaseUrl: '', fetchImpl });

    expect(publishUrl('')).toBe('/v1/publish');
    expect(vi.mocked(fetchImpl).mock.calls[0]![0]).toBe('/v1/publish');
  });

  it('uses the global fetch when no implementation is injected', async () => {
    const globalFetch = fetchReturning(jsonResponse(202, { id: 'post_global', status: 'queued' }));
    vi.stubGlobal('fetch', globalFetch);

    try {
      await expect(submitPost(DRAFT, { apiBaseUrl: '/api' })).resolves.toEqual({
        kind: 'queued',
        id: 'post_global',
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe('submitPost acceptance', () => {
  it('returns the post id from a 202', async () => {
    const fetchImpl = fetchReturning(jsonResponse(202, { id: 'post_abc123', status: 'queued' }));

    await expect(submitPost(DRAFT, { apiBaseUrl: '/api', fetchImpl })).resolves.toEqual({
      kind: 'queued',
      id: 'post_abc123',
    });
  });

  it('reports an error when a success response carries no id', async () => {
    const fetchImpl = fetchReturning(jsonResponse(202, { status: 'queued' }));

    const outcome = await submitPost(DRAFT, { apiBaseUrl: '/api', fetchImpl });

    expect(outcome.kind).toBe('error');
    expect(outcome).toMatchObject({ message: expect.stringContaining('did not return a post id') });
  });
});

describe('submitPost failure mapping', () => {
  it('folds the validation message into an actionable sentence and keeps the request id', async () => {
    const fetchImpl = fetchReturning(
      jsonResponse(400, errorEnvelope('VALIDATION_FAILED', 'content must not be empty')),
    );

    const outcome = await submitPost(DRAFT, { apiBaseUrl: '/api', fetchImpl });

    expect(outcome).toEqual({
      kind: 'error',
      message: 'The API rejected this post. Content must not be empty. Adjust your draft and publish again.',
      requestId: 'req_0f3c',
    });
  });

  it('tells the user to shorten the content on 413', async () => {
    const fetchImpl = fetchReturning(
      jsonResponse(413, errorEnvelope('PAYLOAD_TOO_LARGE', 'request body must not exceed 64kb')),
    );

    const outcome = await submitPost(DRAFT, { apiBaseUrl: '/api', fetchImpl });

    expect(outcome).toMatchObject({ message: expect.stringContaining('Shorten the content') });
  });

  it('says nothing was queued and retrying is safe on 503', async () => {
    const fetchImpl = fetchReturning(
      jsonResponse(503, errorEnvelope('QUEUE_UNAVAILABLE', 'queue unavailable, the post was not accepted')),
    );

    const outcome = await submitPost(DRAFT, { apiBaseUrl: '/api', fetchImpl });

    expect(outcome).toMatchObject({
      kind: 'error',
      message: expect.stringContaining('Nothing was queued'),
      requestId: 'req_0f3c',
    });
    // An infrastructure sentence written for an operator is not repeated to the
    // person who wrote the post.
    expect((outcome as { message: string }).message).not.toContain('queue unavailable');
  });

  it('does not echo the generic server message on 500', async () => {
    const fetchImpl = fetchReturning(
      jsonResponse(500, errorEnvelope('INTERNAL_ERROR', 'internal server error')),
    );

    const outcome = await submitPost(DRAFT, { apiBaseUrl: '/api', fetchImpl });
    const { message } = outcome as { message: string };

    expect(message).toContain('server error 500');
    expect(message).toContain('Your draft has been kept');
    expect(message).not.toContain('internal server error');
  });

  it('points at configuration on 404 and names the URL it called', async () => {
    const fetchImpl = fetchReturning(
      jsonResponse(404, errorEnvelope('NOT_FOUND', 'resource not found'), 'http://web.test/api/v1/publish'),
    );

    const outcome = await submitPost(DRAFT, { apiBaseUrl: '/api', fetchImpl });
    const { message } = outcome as { message: string };

    expect(message).toContain('http://web.test/api/v1/publish');
    expect(message).toContain('misconfigured');
  });

  it('still produces a usable message when the error body is not the envelope', async () => {
    // A proxy answering before the API exists: HTML body, no envelope, no id.
    const fetchImpl = fetchReturning(new Response('<html>502 Bad Gateway</html>', { status: 502 }));

    const outcome = await submitPost(DRAFT, { apiBaseUrl: '/api', fetchImpl });

    expect(outcome).toEqual({
      kind: 'error',
      message: expect.stringContaining('server error 502'),
      requestId: null,
    });
  });

  it('reports unreachability without throwing when fetch rejects', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockRejectedValue(new TypeError('Failed to fetch'));

    const outcome = await submitPost(DRAFT, { apiBaseUrl: '/api', fetchImpl });

    expect(outcome).toEqual({
      kind: 'error',
      message: expect.stringContaining('Could not reach the API at /api'),
      requestId: null,
    });
  });

  it('describes an empty base URL as this site rather than as nothing', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockRejectedValue(new TypeError('Failed to fetch'));

    const outcome = await submitPost(DRAFT, { apiBaseUrl: '', fetchImpl });

    expect(outcome).toMatchObject({ message: expect.stringContaining('Could not reach the API at this site') });
  });

  it('rethrows a caller-initiated abort instead of blaming the network', async () => {
    const controller = new AbortController();
    controller.abort();
    const fetchImpl = vi.fn<typeof fetch>().mockRejectedValue(new DOMException('Aborted', 'AbortError'));

    await expect(
      submitPost(DRAFT, { apiBaseUrl: '/api', fetchImpl, signal: controller.signal }),
    ).rejects.toThrow('Aborted');
  });
});
