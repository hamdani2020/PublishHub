import { describe, expect, it } from 'vitest';

import {
  CONTENT_MAX_LENGTH,
  PLATFORM_ALLOW_LIST,
  PLATFORM_LABELS,
  characterLength,
  hasErrors,
  isPlatform,
  validateDraft,
} from '../publish-rules';
import type { Platform } from '../publish-rules';

/**
 * Draft validation (Requirement 4.1).
 *
 * The cases that matter are the boundaries the API also enforces, because a
 * mismatch either blocks a legal post or lets an illegal one through to a 400.
 */
describe('validateDraft', () => {
  const validDraft = { content: 'Shipping the platform today.', platforms: ['twitter'] as const };

  it('accepts a well-formed draft', () => {
    const errors = validateDraft(validDraft);

    expect(errors).toEqual({ content: null, platforms: null });
    expect(hasErrors(errors)).toBe(false);
  });

  it('rejects empty content', () => {
    const errors = validateDraft({ ...validDraft, content: '' });

    expect(errors.content).toMatch(/enter the content/i);
    expect(errors.platforms).toBeNull();
  });

  it('treats whitespace-only content as empty, matching the API', () => {
    expect(validateDraft({ ...validDraft, content: '   \n\t ' }).content).toMatch(/enter the content/i);
  });

  it('accepts content at exactly the limit and rejects one character past it', () => {
    const atLimit = 'a'.repeat(CONTENT_MAX_LENGTH);

    expect(validateDraft({ ...validDraft, content: atLimit }).content).toBeNull();
    expect(validateDraft({ ...validDraft, content: `${atLimit}a` }).content).toMatch(
      /remove 1 character\./i,
    );
  });

  it('reports how far over the limit the draft is', () => {
    const content = 'a'.repeat(CONTENT_MAX_LENGTH + 12);

    expect(validateDraft({ ...validDraft, content }).content).toMatch(/remove 12 characters\./i);
  });

  it('counts code points, not UTF-16 units, so an emoji is one character', () => {
    // 5000 astral-plane characters is 10000 UTF-16 units. Counting units would
    // reject a draft the API accepts.
    const content = '😀'.repeat(CONTENT_MAX_LENGTH);

    expect(characterLength(content)).toBe(CONTENT_MAX_LENGTH);
    expect(validateDraft({ ...validDraft, content }).content).toBeNull();
  });

  it('rejects an empty platform selection', () => {
    const errors = validateDraft({ ...validDraft, platforms: [] });

    expect(errors.platforms).toMatch(/select at least one platform/i);
    expect(errors.content).toBeNull();
  });

  it('reports both fields at once when both are wrong', () => {
    const errors = validateDraft({ content: '', platforms: [] });

    expect(errors.content).not.toBeNull();
    expect(errors.platforms).not.toBeNull();
    expect(hasErrors(errors)).toBe(true);
  });

  it('accepts every platform in the allow-list, alone and together', () => {
    for (const platform of PLATFORM_ALLOW_LIST) {
      expect(validateDraft({ ...validDraft, platforms: [platform] }).platforms).toBeNull();
    }
    expect(validateDraft({ ...validDraft, platforms: PLATFORM_ALLOW_LIST }).platforms).toBeNull();
  });
});

describe('the platform allow-list', () => {
  it('matches the API allow-list exactly', () => {
    expect([...PLATFORM_ALLOW_LIST]).toEqual(['twitter', 'linkedin', 'mastodon', 'bluesky']);
  });

  it('labels every platform', () => {
    for (const platform of PLATFORM_ALLOW_LIST) {
      expect(PLATFORM_LABELS[platform]).not.toBe('');
    }
    expect(Object.keys(PLATFORM_LABELS)).toHaveLength(PLATFORM_ALLOW_LIST.length);
  });

  it('narrows only allow-listed values', () => {
    expect(isPlatform('twitter')).toBe(true);
    expect(isPlatform('Twitter')).toBe(false);
    expect(isPlatform('threads')).toBe(false);
    expect(isPlatform(undefined)).toBe(false);
    expect(isPlatform(['twitter'])).toBe(false);
  });

  it('keeps the wire value lowercase even where the label is not', () => {
    const linkedin: Platform = 'linkedin';

    expect(PLATFORM_LABELS[linkedin]).toBe('LinkedIn');
  });
});
