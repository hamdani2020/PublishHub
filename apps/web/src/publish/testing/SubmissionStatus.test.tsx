import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { SubmissionStatus } from '../SubmissionStatus';
import { IDLE_STATE } from '../use-publish-submission';

/**
 * The live region's own contract (Requirement 4.6): it exists before there is
 * anything to say, it says nothing while there is nothing to say, and it takes
 * focus every time a result arrives — including a second identical one.
 *
 * The App-level tests cover the same region driven by a real submission; these
 * drive it by state so the edge cases are reachable without a network double.
 */
describe('SubmissionStatus', () => {
  it('renders the live region before any submission so later changes announce', () => {
    render(<SubmissionStatus state={IDLE_STATE} />);

    const region = screen.getByRole('status');
    expect(region).toHaveAttribute('aria-live', 'polite');
    expect(region).toBeEmptyDOMElement();
  });

  it('stays empty while a submission is pending', () => {
    render(<SubmissionStatus state={{ status: 'pending' }} />);

    expect(screen.getByRole('status')).toBeEmptyDOMElement();
  });

  it('clears a previous result when the next submission starts', () => {
    const { rerender } = render(<SubmissionStatus state={{ status: 'queued', id: 'post_first' }} />);
    expect(screen.getByRole('status')).toHaveTextContent('post_first');

    rerender(<SubmissionStatus state={{ status: 'pending' }} />);

    expect(screen.getByRole('status')).toBeEmptyDOMElement();
  });

  it('moves focus into the region when a result arrives', () => {
    const { rerender } = render(<SubmissionStatus state={IDLE_STATE} />);
    expect(document.body).toHaveFocus();

    rerender(<SubmissionStatus state={{ status: 'queued', id: 'post_abc123' }} />);

    const region = screen.getByRole('status');
    expect(region).toContainElement(document.activeElement as HTMLElement);
    expect(document.activeElement).toHaveTextContent('post_abc123');
  });

  it('re-announces an identical result rather than leaving focus behind', () => {
    const { rerender } = render(<SubmissionStatus state={{ status: 'error', message: 'Nope.', requestId: null }} />);
    (document.activeElement as HTMLElement).blur();
    expect(document.body).toHaveFocus();

    // Same values, new object: what a second failed submission produces.
    rerender(<SubmissionStatus state={{ status: 'error', message: 'Nope.', requestId: null }} />);

    expect(screen.getByRole('status')).toContainElement(document.activeElement as HTMLElement);
  });

  it('keeps the result out of the tab order so it is not a stop for everyone else', () => {
    render(<SubmissionStatus state={{ status: 'queued', id: 'post_abc123' }} />);

    expect(document.activeElement).toHaveAttribute('tabindex', '-1');
  });

  it('shows the request id as a quotable reference when the API returned one', () => {
    render(
      <SubmissionStatus state={{ status: 'error', message: 'The API failed.', requestId: 'req_0f3c' }} />,
    );

    const region = screen.getByRole('status');
    expect(region).toHaveTextContent('Post not queued');
    expect(region).toHaveTextContent('Reference: req_0f3c');
  });

  it('omits the reference line when there is no request id', () => {
    render(<SubmissionStatus state={{ status: 'error', message: 'Could not reach the API.', requestId: null }} />);

    expect(screen.getByRole('status')).not.toHaveTextContent('Reference:');
  });

  it('names the outcome in text, not by colour alone', () => {
    const { rerender } = render(<SubmissionStatus state={{ status: 'queued', id: 'post_abc123' }} />);
    expect(screen.getByText('Post queued')).toBeInTheDocument();

    rerender(<SubmissionStatus state={{ status: 'error', message: 'Nope.', requestId: null }} />);
    expect(screen.getByText('Post not queued')).toBeInTheDocument();
  });
});
