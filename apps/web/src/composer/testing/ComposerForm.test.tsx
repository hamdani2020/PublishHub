import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ComposerForm } from '../ComposerForm';
import { CONTENT_MAX_LENGTH } from '../publish-rules';

/**
 * The composer's markup and accessibility wiring (Requirements 4.1, 4.6).
 *
 * Every query here goes through role, label, or accessible description rather
 * than a class name or test id. That is the point: if a control loses its label
 * or an error stops being associated with its field, the query fails, which is
 * the only kind of accessibility assertion that cannot rot into a no-op.
 */

function renderComposer(): { readonly onSubmit: ReturnType<typeof vi.fn> } {
  const onSubmit = vi.fn();
  render(<ComposerForm onSubmit={onSubmit} />);
  return { onSubmit };
}

function contentField(): HTMLTextAreaElement {
  return screen.getByLabelText('Post content');
}

function platformGroup(): HTMLElement {
  return screen.getByRole('group', { name: 'Target platforms' });
}

describe('ComposerForm labels and structure', () => {
  it('renders a content textarea, a platform group, and a publish action', () => {
    renderComposer();

    expect(contentField().tagName).toBe('TEXTAREA');
    expect(platformGroup().tagName).toBe('FIELDSET');
    expect(screen.getByRole('button', { name: 'Publish' })).toHaveAttribute('type', 'submit');
  });

  it('names the form from its heading', () => {
    renderComposer();

    expect(screen.getByRole('form', { name: 'Compose a post' })).toBeInTheDocument();
  });

  it('gives every platform checkbox an accessible label', () => {
    renderComposer();

    const checkboxes = screen.getAllByRole('checkbox');
    expect(checkboxes).toHaveLength(4);
    expect(checkboxes.map((box) => box.getAttribute('value'))).toEqual([
      'twitter',
      'linkedin',
      'mastodon',
      'bluesky',
    ]);

    for (const name of ['Twitter', 'LinkedIn', 'Mastodon', 'Bluesky']) {
      expect(screen.getByRole('checkbox', { name })).toBeInTheDocument();
    }
  });

  it('binds each label to its control rather than wrapping it', () => {
    renderComposer();

    // getByLabelText already proves association, but an explicit id/for check
    // documents that these are real labels and not aria-label strings.
    const textarea = contentField();
    expect(textarea.id).not.toBe('');
    expect(document.querySelector(`label[for="${textarea.id}"]`)).toHaveTextContent('Post content');
  });

  it('describes the content limit before anything is typed', () => {
    renderComposer();

    expect(contentField()).toHaveAccessibleDescription(
      `Up to ${String(CONTENT_MAX_LENGTH)} characters. 0 used.`,
    );
  });

  it('describes the platform group with its own hint', () => {
    renderComposer();

    expect(platformGroup()).toHaveAccessibleDescription('Choose at least one.');
  });

  it('starts with no validation errors and no aria-invalid controls', () => {
    renderComposer();

    expect(contentField()).toHaveAttribute('aria-invalid', 'false');
    expect(screen.queryByText(/enter the content/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/select at least one platform/i)).not.toBeInTheDocument();
  });
});

describe('ComposerForm keyboard operation', () => {
  it('submits a valid draft using the keyboard alone', async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderComposer();

    // Tab from the document into the form: the tab order itself is under test,
    // so nothing is focused or clicked directly.
    await user.tab();
    expect(contentField()).toHaveFocus();
    await user.keyboard('Shipping the platform today.');

    await user.tab();
    expect(screen.getByRole('checkbox', { name: 'Twitter' })).toHaveFocus();
    await user.keyboard(' ');
    expect(screen.getByRole('checkbox', { name: 'Twitter' })).toBeChecked();

    await user.tab();
    await user.tab();
    await user.tab();
    await user.tab();
    expect(screen.getByRole('button', { name: 'Publish' })).toHaveFocus();
    await user.keyboard('{Enter}');

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith({
      content: 'Shipping the platform today.',
      platforms: ['twitter'],
    });
  });

  it('toggles a checkbox off again with the space key', async () => {
    const user = userEvent.setup();
    renderComposer();

    const linkedin = screen.getByRole('checkbox', { name: 'LinkedIn' });
    linkedin.focus();
    await user.keyboard(' ');
    expect(linkedin).toBeChecked();
    await user.keyboard(' ');
    expect(linkedin).not.toBeChecked();
  });

  it('submits platforms in allow-list order however they were selected', async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderComposer();

    await user.type(contentField(), 'Multi-platform post.');
    await user.click(screen.getByRole('checkbox', { name: 'Bluesky' }));
    await user.click(screen.getByRole('checkbox', { name: 'Twitter' }));
    await user.click(screen.getByRole('button', { name: 'Publish' }));

    expect(onSubmit).toHaveBeenCalledWith({
      content: 'Multi-platform post.',
      platforms: ['twitter', 'bluesky'],
    });
  });

  it('does not submit when Enter is pressed inside the textarea', async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderComposer();

    await user.type(contentField(), 'First line{Enter}second line');

    expect(onSubmit).not.toHaveBeenCalled();
    expect(contentField()).toHaveValue('First line\nsecond line');
  });
});

describe('ComposerForm validation messages', () => {
  it('associates the content error with the textarea and does not submit', async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderComposer();

    await user.click(screen.getByRole('checkbox', { name: 'Twitter' }));
    await user.click(screen.getByRole('button', { name: 'Publish' }));

    expect(onSubmit).not.toHaveBeenCalled();

    const textarea = contentField();
    expect(textarea).toHaveAttribute('aria-invalid', 'true');
    expect(textarea).toHaveAccessibleDescription(
      expect.stringContaining('Enter the content you want to publish.'),
    );
    // The error id is in aria-describedby, not merely rendered somewhere nearby.
    expect(textarea.getAttribute('aria-describedby')?.split(' ')).toHaveLength(2);
  });

  it('associates the platform error with the fieldset group', async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderComposer();

    await user.type(contentField(), 'Ready to publish.');
    await user.click(screen.getByRole('button', { name: 'Publish' }));

    expect(onSubmit).not.toHaveBeenCalled();
    expect(platformGroup()).toHaveAccessibleDescription(
      expect.stringContaining('Select at least one platform to publish to.'),
    );
  });

  it('reports both fields when the form is submitted empty', async () => {
    const user = userEvent.setup();
    renderComposer();

    await user.click(screen.getByRole('button', { name: 'Publish' }));

    expect(contentField()).toHaveAccessibleDescription(
      expect.stringContaining('Enter the content you want to publish.'),
    );
    expect(platformGroup()).toHaveAccessibleDescription(
      expect.stringContaining('Select at least one platform to publish to.'),
    );
  });

  it('moves focus to the first invalid control so the message is announced', async () => {
    const user = userEvent.setup();
    renderComposer();

    await user.click(screen.getByRole('button', { name: 'Publish' }));
    expect(contentField()).toHaveFocus();

    // With content valid, the platform group is now the first problem.
    await user.type(contentField(), 'Ready to publish.');
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    expect(screen.getByRole('checkbox', { name: 'Twitter' })).toHaveFocus();
  });

  it('shows no error before the first submit attempt', async () => {
    const user = userEvent.setup();
    renderComposer();

    await user.type(contentField(), 'a');
    await user.clear(contentField());

    expect(contentField()).toHaveAttribute('aria-invalid', 'false');
    expect(screen.queryByText(/enter the content/i)).not.toBeInTheDocument();
  });

  it('clears an error as soon as the field becomes valid', async () => {
    const user = userEvent.setup();
    renderComposer();

    await user.click(screen.getByRole('button', { name: 'Publish' }));
    expect(screen.getByText('Enter the content you want to publish.')).toBeInTheDocument();

    await user.type(contentField(), 'Now it has content.');
    expect(screen.queryByText('Enter the content you want to publish.')).not.toBeInTheDocument();
    expect(contentField()).toHaveAttribute('aria-invalid', 'false');

    await user.click(screen.getByRole('checkbox', { name: 'Mastodon' }));
    expect(screen.queryByText('Select at least one platform to publish to.')).not.toBeInTheDocument();
  });

  it('rejects content past the character limit and says how much to remove', async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderComposer();

    // Pasted rather than typed: 5001 keystrokes through user-event would take
    // longer than the whole suite.
    await user.click(contentField());
    await user.paste('a'.repeat(CONTENT_MAX_LENGTH + 1));
    await user.click(screen.getByRole('checkbox', { name: 'Twitter' }));
    await user.click(screen.getByRole('button', { name: 'Publish' }));

    expect(onSubmit).not.toHaveBeenCalled();
    expect(contentField()).toHaveAccessibleDescription(
      expect.stringContaining(`Content must be ${String(CONTENT_MAX_LENGTH)} characters or fewer. Remove 1 character.`),
    );
  });

  it('keeps the draft intact after a failed submit', async () => {
    const user = userEvent.setup();
    renderComposer();

    await user.type(contentField(), 'Worth keeping.');
    await user.click(screen.getByRole('button', { name: 'Publish' }));

    expect(contentField()).toHaveValue('Worth keeping.');
  });

  it('submits content exactly as typed, without trimming', async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderComposer();

    await user.type(contentField(), '  padded  ');
    await user.click(screen.getByRole('checkbox', { name: 'Twitter' }));
    await user.click(screen.getByRole('button', { name: 'Publish' }));

    expect(onSubmit).toHaveBeenCalledWith({ content: '  padded  ', platforms: ['twitter'] });
  });
});
describe('ComposerForm pending state', () => {
  it('leaves the publish action active by default', () => {
    renderComposer();

    const button = screen.getByRole('button', { name: 'Publish' });
    expect(button).toBeEnabled();
    expect(button).toHaveAttribute('aria-busy', 'false');
  });

  it('disables the action, marks it busy, and changes its label while pending', () => {
    const onSubmit = vi.fn();
    render(<ComposerForm onSubmit={onSubmit} pending />);

    const button = screen.getByRole('button', { name: 'Publishing…' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(screen.queryByRole('button', { name: 'Publish' })).not.toBeInTheDocument();
  });

  it('ignores a submit that arrives while pending, even bypassing the button', () => {
    const onSubmit = vi.fn();
    render(<ComposerForm onSubmit={onSubmit} pending />);

    // A disabled button cannot be clicked, so the guard is tested the only way
    // it can actually be reached: a form submit event.
    fireEvent.submit(screen.getByRole('form', { name: 'Compose a post' }));

    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('keeps the draft editable while a submission is in flight', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<ComposerForm onSubmit={onSubmit} pending />);

    await user.type(contentField(), 'Still typing.');

    expect(contentField()).toHaveValue('Still typing.');
  });
});
