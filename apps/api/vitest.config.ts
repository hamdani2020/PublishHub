import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    // Every test in this package runs against fakes or the shared JSON fixture,
    // so the suite needs no Redis, no AWS account, and no network.
    testTimeout: 10_000,
  },
});
