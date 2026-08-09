import { describe, expect, it, vi } from 'vitest';

import { POSTS_PATH, RECENT_POSTS_LIMIT, fetchRecentPosts, postsUrl } from '../posts-client';

/**
 * The recent-posts read and its outcome mapping (Requirement 4.5).
 *
 * `fetch` is the only double, and only because a unit test has no API to talk to;
 * the real `Response` carries the status and the parse behaviour, so the mapping
 * runs against genuine responses.
 *
 * Two invariants hold throughout: an outcome is always returned rather than
 * thrown, and an empty list is a success and not a failure.
 */

const LIST_URL = `/api${POSTS_PATH}?limit=${String(RECENT_POSTS_LIMIT)}`;

function record(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'post_01HRC0000000000000000001',
    content: 'Shipping the platform today.',
    platforms: ['twitter'],
    status: 'queued',
    job_id: 'job_01HRC0000000000000000001',
    created_at: '2025-01-01T00:00:00.000Z',
    updated_at: '2025-01-01T00:00:00.000Z',
    ...overrides,
  };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function fetchReturning(response: Response): typeof fetch {
  return vi.fn<typeof fetch>().mockResolvedValue(response);
}

describe('fetchRecentPosts request', () => {
  it('gets the versioned posts path with a bounded limit', async () => {
    const fetchImpl = fetchReturning(jsonResponse(200, { posts: [], count: 0, limit: 10 }));

    await fetchRecentPosts({ apiBaseUrl: '/api', fetchImpl });

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = vi.mocked(fetchImpl).mock.calls[0]!;
    expect(url).toBe(LIST_URL);
    expect(init?.method).toBe('GET');
  });

  it('builds a same-origin URL when the base URL is empty', () => {
    expect(postsUrl('')).toBe(`${POSTS_PATH}?limit=${String(RECENT_POSTS_LIMIT)}`);
  });
});

describe('fetchRecentPosts success', () => {
  it('keeps the fields the list renders, in the order the API returned them', async () => {
    const fetchImpl = fetchReturning(
      jsonResponse(200, {
        posts: [
          record({ id: 'post_newest', status: 'published', platforms: ['twitter', 'bluesky'] }),
          record({ id: 'post_older' }),
        ],
        count: 2,
        limit: 10,
      }),
    );

    const outcome = await fetchRecentPosts({ apiBaseUrl: '/api', fetchImpl });

    expect(outcome).toEqual({
      kind: 'posts',
      posts: [
        {
          id: 'post_newest',
          content: 'Shipping the platform today.',
          platforms: ['twitter', 'bluesky'],
          status: 'published',
        },
        {
          id: 'post_older',
          content: 'Shipping the platform today.',
          platforms: ['twitter'],
          status: 'queued',
        },
      ],
    });
  });

  it('treats an empty list as a success', async () => {
    const fetchImpl = fetchReturning(jsonResponse(200, { posts: [], count: 0, limit: 10 }));

    await expect(fetchRecentPosts({ apiBaseUrl: '/api', fetchImpl })).resolves.toEqual({
      kind: 'posts',
      posts: [],
    });
  });

  it('accepts a status it has never seen rather than dropping the post', async () => {
    const fetchImpl = fetchReturning(
      jsonResponse(200, { posts: [record({ status: 'retry_scheduled' })], count: 1, limit: 10 }),
    );

    const outcome = await fetchRecentPosts({ apiBaseUrl: '/api', fetchImpl });

    expect(outcome).toMatchObject({ kind: 'posts', posts: [{ status: 'retry_scheduled' }] });
  });

  it('skips an unreadable entry instead of blanking the whole list', async () => {
    const fetchImpl = fetchReturning(
      jsonResponse(200, {
        // No id, and a content field that is not a string: neither is a record.
        posts: [record(), { content: 'orphan' }, record({ id: 'post_third', content: 42 })],
        count: 3,
        limit: 10,
      }),
    );

    const outcome = await fetchRecentPosts({ apiBaseUrl: '/api', fetchImpl });

    // Exactly the one readable record survives; the two broken entries are gone
    // rather than rendered as blanks.
    expect(outcome).toEqual({
      kind: 'posts',
      posts: [
        {
          id: 'post_01HRC0000000000000000001',
          content: 'Shipping the platform today.',
          platforms: ['twitter'],
          status: 'queued',
        },
      ],
    });
  });

  it('reports an error when the body is not a list envelope', async () => {
    const fetchImpl = fetchReturning(jsonResponse(200, { count: 0 }));

    const outcome = await fetchRecentPosts({ apiBaseUrl: '/api', fetchImpl });

    expect(outcome).toMatchObject({
      kind: 'error',
      message: expect.stringContaining('shape this page does not understand'),
    });
  });
});

describe('fetchRecentPosts failure mapping', () => {
  function envelope(code: string, message: string): unknown {
    return { error: { code, message, request_id: 'req_0f3c' } };
  }

  it('says posting still works when the store is unavailable', async () => {
    const fetchImpl = fetchReturning(
      jsonResponse(503, envelope('DEPENDENCY_UNAVAILABLE', 'post store unavailable')),
    );

    const outcome = await fetchRecentPosts({ apiBaseUrl: '/api', fetchImpl });

    expect(outcome).toMatchObject({
      kind: 'error',
      message: expect.stringContaining('Posting still works'),
      requestId: 'req_0f3c',
    });
    // An operator's sentence about a store is not repeated to the reader.
    expect((outcome as { message: string }).message).not.toContain('post store unavailable');
  });

  it('points at configuration on 404', async () => {
    const fetchImpl = fetchReturning(jsonResponse(404, envelope('NOT_FOUND', 'resource not found')));

    const outcome = await fetchRecentPosts({ apiBaseUrl: '/api', fetchImpl });

    expect(outcome).toMatchObject({ message: expect.stringContaining('misconfigured') });
  });

  it('still produces a usable message when the error body is not the envelope', async () => {
    const fetchImpl = fetchReturning(new Response('<html>502 Bad Gateway</html>', { status: 502 }));

    const outcome = await fetchRecentPosts({ apiBaseUrl: '/api', fetchImpl });

    expect(outcome).toEqual({
      kind: 'error',
      message: expect.stringContaining('server error 502'),
      requestId: null,
    });
  });

  it('reports unreachability without throwing when fetch rejects', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockRejectedValue(new TypeError('Failed to fetch'));

    const outcome = await fetchRecentPosts({ apiBaseUrl: '/api', fetchImpl });

    expect(outcome).toEqual({
      kind: 'error',
      message: expect.stringContaining('Could not reach the API at /api'),
      requestId: null,
    });
  });

  it('rethrows a caller-initiated abort instead of blaming the network', async () => {
    const controller = new AbortController();
    controller.abort();
    const fetchImpl = vi.fn<typeof fetch>().mockRejectedValue(new DOMException('Aborted', 'AbortError'));

    await expect(
      fetchRecentPosts({ apiBaseUrl: '/api', fetchImpl, signal: controller.signal }),
    ).rejects.toThrow('Aborted');
  });
});
