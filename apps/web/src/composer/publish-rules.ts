/**
 * Client-side rules for the composer draft.
 *
 * These are the API's rules restated for the browser, not a second opinion:
 * `content` is a non-empty string of at most 5000 characters, and `platforms` is
 * a non-empty selection from the allow-list, exactly as
 * `apps/api/src/posts/publish-schema.ts` enforces and `docs/message-schema.md`
 * pins. Client validation is a convenience — it turns a round-trip into instant
 * feedback — and the server stays the authority, so a drift here costs a 400
 * rather than bad data.
 *
 * They are duplicated rather than imported because there is no shared TypeScript
 * package between `apps/api` and `apps/web` (the API compiles to Node ESM, the
 * web app is a Vite bundle). If a third consumer appears, extracting a package
 * beats a third copy.
 */

/** Mirrors `PLATFORM_ALLOW_LIST` in the API. Order is the render order. */
export const PLATFORM_ALLOW_LIST = ['twitter', 'linkedin', 'mastodon', 'bluesky'] as const;

export type Platform = (typeof PLATFORM_ALLOW_LIST)[number];

/**
 * Display names. The wire value stays lowercase; only the label is cased, so a
 * checkbox reads "LinkedIn" while the request still carries `linkedin`.
 */
export const PLATFORM_LABELS: Readonly<Record<Platform, string>> = Object.freeze({
  twitter: 'Twitter',
  linkedin: 'LinkedIn',
  mastodon: 'Mastodon',
  bluesky: 'Bluesky',
});

export const CONTENT_MAX_LENGTH = 5000;

/** What the user has composed so far. The submitted payload shape, minus nothing. */
export interface ComposerDraft {
  readonly content: string;
  readonly platforms: readonly Platform[];
}

/**
 * One message per field, or `null` when the field is fine.
 *
 * Both keys are always present rather than optional: a caller reading
 * `errors.platforms` cannot forget the field exists, and `exactOptionalPropertyTypes`
 * makes optional-and-absent an awkward thing to build incrementally.
 */
export interface DraftErrors {
  readonly content: string | null;
  readonly platforms: string | null;
}

export const NO_ERRORS: DraftErrors = Object.freeze({ content: null, platforms: null });

/**
 * Length in Unicode code points, not UTF-16 code units, matching the API's
 * `characterLength`. Without this an emoji would count as two characters here
 * and one there, and a draft that looks legal would come back as a 400.
 */
export function characterLength(value: string): number {
  return Array.from(value).length;
}

export function isPlatform(value: unknown): value is Platform {
  return typeof value === 'string' && (PLATFORM_ALLOW_LIST as readonly string[]).includes(value);
}

/**
 * Validate a draft.
 *
 * Messages are written for a person rather than copied from the API envelope:
 * they name the field, say what is wrong, and say what to do about it, because
 * these strings are read by a screen reader as the field's description.
 */
export function validateDraft(draft: ComposerDraft): DraftErrors {
  return {
    content: validateContent(draft.content),
    platforms: validatePlatforms(draft.platforms),
  };
}

export function hasErrors(errors: DraftErrors): boolean {
  return errors.content !== null || errors.platforms !== null;
}

function validateContent(content: string): string | null {
  // Blank-after-trimming counts as empty, as on the server: a post of spaces is
  // not a post. The draft itself is never rewritten — trimming only decides.
  if (content.trim() === '') {
    return 'Enter the content you want to publish.';
  }

  const length = characterLength(content);
  if (length > CONTENT_MAX_LENGTH) {
    const over = length - CONTENT_MAX_LENGTH;
    return `Content must be ${String(CONTENT_MAX_LENGTH)} characters or fewer. Remove ${String(over)} ${
      over === 1 ? 'character' : 'characters'
    }.`;
  }

  return null;
}

function validatePlatforms(platforms: readonly Platform[]): string | null {
  if (platforms.length === 0) {
    return 'Select at least one platform to publish to.';
  }
  return null;
}
