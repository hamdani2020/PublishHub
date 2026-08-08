/**
 * Loader for the shared contract fixture.
 *
 * `contracts/publish-job.v1.fixture.json` is read from the repository root, the
 * same bytes the Python suite reads, which is what stops the two
 * implementations from drifting (docs/message-schema.md).
 */

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export interface FixtureVariant {
  name: string;
  description: string;
  message: Record<string, unknown>;
}

export interface FixtureInvalid {
  name: string;
  reason: string;
  message?: Record<string, unknown>;
  /** Present instead of `message` when the payload is not valid JSON at all. */
  raw?: string;
}

export interface PublishJobFixture {
  schema_version: number;
  required_fields: string[];
  constraints: {
    content_min_length: number;
    content_max_length: number;
    platform_allow_list: string[];
    platforms_min_items: number;
    platforms_unique: boolean;
    attempt_min: number;
    patterns: { job_id: string; post_id: string; enqueued_at: string };
    dead_letter_reasons: string[];
  };
  canonical: Record<string, unknown>;
  variants: FixtureVariant[];
  invalid: FixtureInvalid[];
}

// apps/api/src/queue/testing -> repository root
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../..');

export const FIXTURE_PATH = resolve(REPO_ROOT, 'contracts/publish-job.v1.fixture.json');

export function loadPublishJobFixture(): PublishJobFixture {
  return JSON.parse(readFileSync(FIXTURE_PATH, 'utf8')) as PublishJobFixture;
}
