/**
 * Post identifier generation.
 *
 * `docs/message-schema.md` pins the format: `post_` followed by a 26-character
 * Crockford base32 ULID, matching `^post_[0-9A-HJKMNP-TV-Z]{26}$`. The same
 * value is the key of the Redis post record and the `id` returned to the client
 * by `POST /api/v1/publish`, so it has to be safe in a URL, safe in a Redis key,
 * and readable over the phone.
 *
 * A ULID rather than a UUID because the first 10 characters encode the
 * millisecond timestamp: ids sort lexicographically by creation time, which
 * makes a recent-posts listing orderable without a second index and makes a log
 * line self-dating. Crockford base32 drops `I`, `L`, `O`, and `U`, which removes
 * the digit-letter confusions and the accidental profanity.
 *
 * ULID is implemented here rather than pulled in as a dependency: it is 20 lines,
 * and the format is already fixed by the message schema, so a library would add
 * a supply-chain surface without removing any decision.
 */

import { randomBytes } from 'node:crypto';

/** Crockford base32: 32 symbols, no I, L, O, or U. */
const ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';

/** Every id carries this prefix, so an id is self-describing in a log or a URL. */
export const POST_ID_PREFIX = 'post_';

/** 10 base32 characters hold 50 bits, comfortably above the 48-bit ULID time. */
const TIME_LENGTH = 10;
/** 16 base32 characters hold 80 bits of randomness, as the ULID spec requires. */
const RANDOM_LENGTH = 16;

/** Largest instant a 48-bit ULID timestamp can express: 10889-08-02. */
export const MAX_ULID_TIME_MS = 281_474_976_710_655;

function encodeTime(milliseconds: number): string {
  if (!Number.isInteger(milliseconds) || milliseconds < 0 || milliseconds > MAX_ULID_TIME_MS) {
    throw new RangeError(
      `cannot encode ${String(milliseconds)} as a ULID timestamp: must be an integer between 0 and ${String(MAX_ULID_TIME_MS)}`,
    );
  }
  let remaining = milliseconds;
  let encoded = '';
  for (let position = 0; position < TIME_LENGTH; position += 1) {
    encoded = ALPHABET[remaining % 32] + encoded;
    remaining = Math.floor(remaining / 32);
  }
  return encoded;
}

function encodeRandom(): string {
  // 256 is an exact multiple of 32, so `byte % 32` is uniform over the alphabet
  // — no modulo bias to correct for.
  const bytes = randomBytes(RANDOM_LENGTH);
  let encoded = '';
  for (const byte of bytes) {
    encoded += ALPHABET[byte % 32];
  }
  return encoded;
}

/**
 * Generate a post id. `date` is injectable so a test can assert the time
 * ordering rather than assume it.
 */
export function generatePostId(date: Date = new Date()): string {
  return `${POST_ID_PREFIX}${encodeTime(date.getTime())}${encodeRandom()}`;
}
