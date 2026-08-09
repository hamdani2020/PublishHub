import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

/**
 * Test configuration, kept in its own file to match the API package.
 *
 * Vitest reads this instead of `vite.config.ts` when both exist, so the React
 * plugin is declared again here rather than inherited — an explicit duplicate
 * beats a config merge that silently drops the dev-server proxy into the test
 * environment.
 *
 * Tests run against jsdom and touch no network: nothing in this suite needs the
 * API, Redis, or a queue.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: false,
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    setupFiles: ['src/testing/setup.ts'],
    testTimeout: 10_000,
    restoreMocks: true,
  },
});
