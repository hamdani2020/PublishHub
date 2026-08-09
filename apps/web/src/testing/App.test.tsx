import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { App } from '../App';

/**
 * Shell rendering and the two lifecycles it owns.
 *
 * The composer's own behaviour is covered in `src/composer/testing`, the posts
 * panel's in `src/posts/testing`; what matters here is the wiring — a click
 * reaching the network, a response reaching the live region, a queued post
 * reaching the list.
 *
 * `fetch` is the only double, and it is routed by URL rather than by call order:
 * the shell calls two endpoints on its own schedule, and a mock that answered
 * whichever request happened to arrive first would make these tests depend on
 * that schedule.
 */

const EMPTY_LIST_MESSAGE = 'No posts yet. Publish a post and it will appear here.';

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function postList(posts: readonly unknown[]): Response {
  return json(200, { posts, count: posts.length, limit: 10 });
}

function record(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'post_01HRC0000000000000000001',
    content: 'An earlier post.',
    platforms: ['twitter'],
    status: 'published',
    job_id: 'job_01HRC0000000000000000001',
    created_at: '2025-01-01T00:00:00.000Z',
    updated_at: '2025-01-01T00:00:00.000Z',
    ...overrides,
  };
}

interface Routes {
  /** Answers `POST /v1/publish`. Defaults to an accepted post. */
  readonly publish?: () => Promise<Response>;
  /** Answers `GET /v1/posts`. Defaults to an empty list. */
  readonly posts?: () => Promise<Response>;
}

/**
 * Stub `fetch` for both endpoints. Handlers are functions, not responses,
 * because a `Response` body can only be read once and the posts endpoint is
 * called again after every successful publish.
 */
function stubFetch(routes: Routes = {}): ReturnType<typeof vi.fn> {
  const publish = routes.publish ?? (() => Promise.resolve(json(202, { id: 'post_default', status: 'queued' })));
  const posts = routes.posts ?? (() => Promise.resolve(postList([])));

  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = requestUrl(input);
    if (url.includes('/v1/publish')) {
      return publish();
    }
    if (url.includes('/v1/posts')) {
      return posts();
    }
    return Promise.reject(new Error(`unexpected request: ${url}`));
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** The three shapes `fetch` accepts, narrowed to the URL each one carries. */
function requestUrl(input: unknown): string {
  if (typeof input === 'string') {
    return input;
  }
  if (input instanceof URL) {
    return input.href;
  }
  return input instanceof Request ? input.url : '';
}

/** The parsed JSON body of a recorded call, or `undefined` when there was none. */
function requestBody(init: unknown): unknown {
  const body = (init as RequestInit | undefined)?.body;
  return typeof body === 'string' ? (JSON.parse(body) as unknown) : undefined;
}

function callsTo(fetchMock: ReturnType<typeof vi.fn>, path: string): unknown[][] {
  return fetchMock.mock.calls.filter(([input]) => requestUrl(input).includes(path));
}

/**
 * Queries scoped to the posts panel. The composer renders its platform
 * checkboxes as a list too, so an unscoped `getAllByRole('listitem')` would
 * count those as posts.
 */
function postRows(): HTMLElement[] {
  return within(screen.getByRole('region', { name: 'Recent posts' })).getAllByRole('listitem');
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('App', () => {
  it('renders the page heading and main landmark', async () => {
    stubFetch();
    render(<App config={{ apiBaseUrl: '/api' }} />);

    expect(screen.getByRole('heading', { level: 1, name: 'PublishHub' })).toBeInTheDocument();
    expect(screen.getByRole('main')).toBeInTheDocument();
    await screen.findByText(EMPTY_LIST_MESSAGE);
  });

  it('mounts the composer and the recent posts panel inside the main landmark', async () => {
    stubFetch();
    render(<App config={{ apiBaseUrl: '/api' }} />);

    const main = screen.getByRole('main');
    expect(main).toContainElement(screen.getByRole('form', { name: 'Compose a post' }));
    expect(main).toContainElement(screen.getByRole('region', { name: 'Recent posts' }));
    await screen.findByText(EMPTY_LIST_MESSAGE);
  });

  it('shows the resolved API base URL', async () => {
    stubFetch();
    render(<App config={{ apiBaseUrl: 'https://api.example.com' }} />);

    expect(screen.getByText('https://api.example.com')).toBeInTheDocument();
    await screen.findByText(EMPTY_LIST_MESSAGE);
  });

  it('describes an empty base URL as same origin rather than showing nothing', async () => {
    stubFetch();
    render(<App config={{ apiBaseUrl: '' }} />);

    expect(screen.getByText('/ (same origin)')).toBeInTheDocument();
    await screen.findByText(EMPTY_LIST_MESSAGE);
  });
});

/**
 * The submission lifecycle end to end through the shell (Requirements 4.2, 4.3,
 * 4.4, 4.6).
 *
 * These tests drive the real composer, the real hook, and the real publish
 * client. That is deliberate — the behaviours under test are all seams between
 * those pieces, and a test that stubbed the hook would prove none of them.
 */
describe('App submission', () => {
  /** A response the test resolves on demand, so the in-flight state is observable. */
  function deferredPublish(): {
    readonly fetchMock: ReturnType<typeof vi.fn>;
    readonly settle: (response: Response) => void;
  } {
    let settle = (_response: Response): void => undefined;
    const pending = new Promise<Response>((resolve) => {
      settle = resolve;
    });
    const fetchMock = stubFetch({ publish: () => pending });
    return { fetchMock, settle };
  }

  async function composeValidDraft(user: ReturnType<typeof userEvent.setup>): Promise<void> {
    await user.type(screen.getByLabelText('Post content'), 'Shipping the platform today.');
    await user.click(screen.getByRole('checkbox', { name: 'Twitter' }));
  }

  it('calls the publish endpoint with the composed draft', async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch({
      publish: () => Promise.resolve(json(202, { id: 'post_abc123', status: 'queued' })),
    });
    render(<App config={{ apiBaseUrl: '/api' }} />);

    await composeValidDraft(user);
    await user.click(screen.getByRole('button', { name: 'Publish' }));

    await screen.findByText('Post queued');
    const publishCalls = callsTo(fetchMock, '/v1/publish');
    expect(publishCalls).toHaveLength(1);
    const [url, init] = publishCalls[0]!;
    expect(requestUrl(url)).toBe('/api/v1/publish');
    expect(requestBody(init)).toEqual({
      content: 'Shipping the platform today.',
      platforms: ['twitter'],
    });
  });

  it('shows a pending state and refuses a duplicate submission while in flight', async () => {
    const user = userEvent.setup();
    const { fetchMock, settle } = deferredPublish();
    render(<App config={{ apiBaseUrl: '/api' }} />);

    await composeValidDraft(user);
    await user.click(screen.getByRole('button', { name: 'Publish' }));

    const button = screen.getByRole('button', { name: 'Publishing…' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');

    // A second attempt during the flight, both through the control and by
    // submitting the form directly, which a disabled button cannot prevent.
    await user.click(button);
    fireEvent.submit(screen.getByRole('form', { name: 'Compose a post' }));
    expect(callsTo(fetchMock, '/v1/publish')).toHaveLength(1);

    settle(json(202, { id: 'post_abc123', status: 'queued' }));

    await screen.findByText('Post queued');
    const settled = screen.getByRole('button', { name: 'Publish' });
    expect(settled).toBeEnabled();
    expect(settled).toHaveAttribute('aria-busy', 'false');
  });

  it('announces the queued confirmation with the returned post id and moves focus to it', async () => {
    const user = userEvent.setup();
    stubFetch({ publish: () => Promise.resolve(json(202, { id: 'post_abc123', status: 'queued' })) });
    render(<App config={{ apiBaseUrl: '/api' }} />);

    const region = screen.getByRole('status');
    expect(region).toHaveAttribute('aria-live', 'polite');
    expect(region).toBeEmptyDOMElement();

    await composeValidDraft(user);
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    await screen.findByText('Post queued');

    expect(region).toHaveTextContent('post_abc123');
    expect(region).toContainElement(document.activeElement as HTMLElement);
  });

  it('keeps the draft intact and explains what to do when the API rejects the post', async () => {
    const user = userEvent.setup();
    stubFetch({
      publish: () =>
        Promise.resolve(
          json(503, {
            error: {
              code: 'QUEUE_UNAVAILABLE',
              message: 'queue unavailable, the post was not accepted',
              request_id: 'req_0f3c',
            },
          }),
        ),
    });
    render(<App config={{ apiBaseUrl: '/api' }} />);

    await composeValidDraft(user);
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    await screen.findByText('Post not queued');

    // The draft — content and selection both — survives the failure.
    expect(screen.getByLabelText('Post content')).toHaveValue('Shipping the platform today.');
    expect(screen.getByRole('checkbox', { name: 'Twitter' })).toBeChecked();

    const region = screen.getByRole('status');
    expect(region).toHaveTextContent('Nothing was queued');
    expect(region).toHaveTextContent('Reference: req_0f3c');
    // Retrying has to be possible, which means the button is usable again.
    expect(screen.getByRole('button', { name: 'Publish' })).toBeEnabled();
  });

  it('keeps the draft intact when the API is unreachable', async () => {
    const user = userEvent.setup();
    stubFetch({ publish: () => Promise.reject(new TypeError('Failed to fetch')) });
    render(<App config={{ apiBaseUrl: '/api' }} />);

    await composeValidDraft(user);
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    await screen.findByText('Post not queued');

    expect(screen.getByLabelText('Post content')).toHaveValue('Shipping the platform today.');
    expect(screen.getByRole('status')).toHaveTextContent('Could not reach the API at /api');
  });

  it('does not call the publish endpoint when client validation fails', async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch();
    render(<App config={{ apiBaseUrl: '/api' }} />);

    // No platform selected: the composer stops this before the network.
    await user.type(screen.getByLabelText('Post content'), 'Shipping the platform today.');
    await user.click(screen.getByRole('button', { name: 'Publish' }));

    expect(callsTo(fetchMock, '/v1/publish')).toHaveLength(0);
    expect(screen.getByRole('status')).toBeEmptyDOMElement();
  });

  it('replaces a previous failure with the confirmation on a successful retry', async () => {
    const user = userEvent.setup();
    let attempt = 0;
    stubFetch({
      publish: () => {
        attempt += 1;
        return attempt === 1
          ? Promise.reject(new TypeError('Failed to fetch'))
          : Promise.resolve(json(202, { id: 'post_retry', status: 'queued' }));
      },
    });
    render(<App config={{ apiBaseUrl: '/api' }} />);

    await composeValidDraft(user);
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    await screen.findByText('Post not queued');

    await user.click(screen.getByRole('button', { name: 'Publish' }));
    await screen.findByText('Post queued');

    const region = screen.getByRole('status');
    expect(region).toHaveTextContent('post_retry');
    expect(region).not.toHaveTextContent('Post not queued');
  });
});

/**
 * The recent posts panel as the shell wires it (Requirement 4.5).
 *
 * The panel's own states are covered directly in `src/posts/testing`; what is
 * checked here is that it loads from the API on mount and reloads after a post is
 * queued, which is the only moment the list is certainly stale.
 */
describe('App recent posts', () => {
  it('loads recent posts from the API on mount', async () => {
    const fetchMock = stubFetch({
      posts: () =>
        Promise.resolve(
          postList([
            record({ id: 'post_one', content: 'An earlier post.', status: 'published' }),
            record({ id: 'post_two', content: 'Older still.', status: 'failed' }),
          ]),
        ),
    });
    render(<App config={{ apiBaseUrl: '/api' }} />);

    await screen.findByText('Showing 2 recent posts, newest first.');
    expect(postRows()).toHaveLength(2);
    expect(screen.getByText('post_one')).toBeInTheDocument();
    expect(callsTo(fetchMock, '/v1/posts')).toHaveLength(1);
    expect(requestUrl(callsTo(fetchMock, '/v1/posts')[0]![0])).toBe('/api/v1/posts?limit=10');
  });

  it('reloads the list after a post is queued so the new post appears', async () => {
    const user = userEvent.setup();
    let listRequests = 0;
    const fetchMock = stubFetch({
      publish: () => Promise.resolve(json(202, { id: 'post_fresh', status: 'queued' })),
      posts: () => {
        listRequests += 1;
        return Promise.resolve(
          listRequests === 1
            ? postList([])
            : postList([record({ id: 'post_fresh', content: 'Shipping the platform today.', status: 'queued' })]),
        );
      },
    });
    render(<App config={{ apiBaseUrl: '/api' }} />);

    await screen.findByText(EMPTY_LIST_MESSAGE);

    await user.type(screen.getByLabelText('Post content'), 'Shipping the platform today.');
    await user.click(screen.getByRole('checkbox', { name: 'Twitter' }));
    await user.click(screen.getByRole('button', { name: 'Publish' }));

    await screen.findByText('Showing 1 recent post, newest first.');
    const rows = postRows();
    expect(rows).toHaveLength(1);
    const row = rows[0]!;
    expect(row).toHaveTextContent('post_fresh');
    expect(row).toHaveTextContent('Queued');
    expect(callsTo(fetchMock, '/v1/posts')).toHaveLength(2);
  });

  it('reports a failed load without affecting the composer', async () => {
    stubFetch({ posts: () => Promise.reject(new TypeError('Failed to fetch')) });
    render(<App config={{ apiBaseUrl: '/api' }} />);

    await screen.findByText(/Could not reach the API at \/api, so recent posts are unavailable/);
    // A read failure must not take the write path with it.
    expect(screen.getByRole('button', { name: 'Publish' })).toBeEnabled();
  });
});
