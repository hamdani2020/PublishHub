// Vitest setup, run once per test file.
//
// `jest-dom/vitest` registers the DOM matchers (`toBeInTheDocument`,
// `toHaveAccessibleDescription`, and the rest) on Vitest's `expect`, which the
// accessibility assertions in spec tasks 5.2 to 5.4 rely on.
import '@testing-library/jest-dom/vitest';

import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// `globals: false` in vitest.config.ts means Testing Library's automatic cleanup
// does not register itself, so it is wired up explicitly. Without this, a
// rendered tree leaks into the next test's queries.
afterEach(() => {
  cleanup();
});
