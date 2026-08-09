import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { CONTENT_PREVIEW_MAX } from '../post-display';
import type { PostSummary } from '../posts-client';
import { RecentPosts } from '../RecentPosts';
import type { RecentPostsState } from '../use-recent-posts';

/**
 * The recent posts panel (Requirements 4.5, 4.6).
 *
 * The component takes its state as a prop, so every state is rendered directly
 * with no network double. The assertions are about what a reader — sighted or
 * using a screen reader — can actually get out of the panel: the list semantics,
 * each value paired with its label, and one sentence saying which state the panel
 * is in.
 */

function post(overrides: Partial<PostSummary> = {}): PostSummary {
  return {
    id: 'post_01HRC0000000000000000001',
    content: 'Shipping the platform today.',
    platforms: ['twitter', 'linkedin'],
    status: 'queued',
    ...overrides,
  };
}

function ready(posts: readonly PostSummary[]): RecentPostsState {
  return { status: 'ready', posts };
}

describe('RecentPosts populated', () => {
  it('renders every post as a list item with id, content, platforms, and status', () => {
    render(
      <RecentPosts
        state={ready([
          post({ id: 'post_newest', content: 'Newest post.', status: 'published' }),
          post({ id: 'post_older', content: 'Older post.', platforms: ['mastodon'], status: 'failed' }),
        ])}
      />,
    );

    const items = screen.getAllByRole('listitem');
    expect(items).toHaveLength(2);

    // Newest first, as the API returned them: order is information here.
    expect(items[0]).toHaveTextContent('post_newest');
    expect(items[1]).toHaveTextContent('post_older');

    // Every value arrives with its label, so "Failed" cannot be read as a bare
    // word whose meaning depends on the column it landed in.
    expect(items[0]).toHaveTextContent('Content');
    expect(items[0]).toHaveTextContent('Newest post.');
    expect(items[0]).toHaveTextContent('Platforms');
    expect(items[0]).toHaveTextContent('Twitter, LinkedIn');
    expect(items[0]).toHaveTextContent('Status');
    expect(items[0]).toHaveTextContent('Published');

    expect(items[1]).toHaveTextContent('Mastodon');
    expect(items[1]).toHaveTextContent('Failed');
  });

  it('exposes the rows as a single list under a named region', () => {
    render(<RecentPosts state={ready([post(), post({ id: 'post_second' })])} />);

    const region = screen.getByRole('region', { name: 'Recent posts' });
    const list = screen.getByRole('list');
    expect(region).toContainElement(list);
    // One list, so assistive technology announces "2 items" once rather than
    // walking two nested lists.
    expect(screen.getAllByRole('list')).toHaveLength(1);
  });

  it('summarizes the count in a polite live region', () => {
    render(<RecentPosts state={ready([post(), post({ id: 'post_second' })])} />);

    const status = screen.getByText('Showing 2 recent posts, newest first.');
    expect(status).toHaveAttribute('aria-live', 'polite');
  });

  it('truncates a long post and says so for a screen reader', () => {
    const long = 'a'.repeat(CONTENT_PREVIEW_MAX + 50);
    render(<RecentPosts state={ready([post({ content: long })])} />);

    const item = screen.getByRole('listitem');
    expect(item).toHaveTextContent(`${'a'.repeat(CONTENT_PREVIEW_MAX)}…`);
    expect(item).not.toHaveTextContent(long);
    // The ellipsis is a visual cue only, so the fact of truncation is also text.
    expect(item).toHaveTextContent('(shortened for this list)');
  });

  it('labels a status it does not recognise rather than dropping the post', () => {
    render(<RecentPosts state={ready([post({ id: 'post_future', status: 'retry_scheduled' })])} />);

    const item = screen.getByRole('listitem');
    expect(item).toHaveTextContent('post_future');
    expect(item).toHaveTextContent('Retry scheduled');
  });
});

describe('RecentPosts empty', () => {
  it('says there are no posts yet instead of rendering an empty list', () => {
    render(<RecentPosts state={ready([])} />);

    expect(
      screen.getByText('No posts yet. Publish a post and it will appear here.'),
    ).toHaveAttribute('aria-live', 'polite');
    // An empty <ul> would announce "list, 0 items", which is noise, not news.
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
    expect(screen.queryByRole('listitem')).not.toBeInTheDocument();
  });

  it('still exposes the named region and the refresh control', () => {
    render(<RecentPosts state={ready([])} onRefresh={vi.fn()} />);

    expect(screen.getByRole('region', { name: 'Recent posts' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
  });
});

describe('RecentPosts loading and failure', () => {
  it('reports loading and marks the refresh control busy', () => {
    render(<RecentPosts state={{ status: 'loading' }} onRefresh={vi.fn()} />);

    expect(screen.getByText('Loading recent posts…')).toBeInTheDocument();
    const button = screen.getByRole('button', { name: 'Refresh' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');
  });

  it('shows the failure message and the reference to quote', () => {
    render(
      <RecentPosts
        state={{
          status: 'error',
          message: 'Recent posts are temporarily unavailable.',
          requestId: 'req_0f3c',
        }}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText('Recent posts are temporarily unavailable.')).toHaveAttribute(
      'aria-live',
      'polite',
    );
    expect(screen.getByText('req_0f3c')).toBeInTheDocument();
    // A failure must not leave the reader stuck: retrying is still possible.
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
  });

  it('calls back when the reader asks for a refresh', async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn();
    render(<RecentPosts state={ready([post()])} onRefresh={onRefresh} />);

    await user.click(screen.getByRole('button', { name: 'Refresh' }));

    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('omits the refresh control when no handler is supplied', () => {
    render(<RecentPosts state={ready([post()])} />);

    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
