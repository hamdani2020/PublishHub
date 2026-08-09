import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

/**
 * Dev server and build configuration.
 *
 * The `/api` proxy is what makes the default runtime configuration honest: in
 * development the dev server forwards `/api` to the local API, and in the
 * container nginx forwards it to the API Service (spec task 7.2). Both are
 * same-origin, so the app never needs a different base URL per environment and
 * CORS stays out of the picture (Requirement 4.7).
 *
 * Port 3000 matches the API's default `CORS_ORIGINS` value, so a developer who
 * bypasses the proxy still gets a working origin.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.VITE_DEV_API_TARGET ?? 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    // The Dockerfile serves this directory from nginx; a source map per chunk is
    // cheap and makes a production stack trace readable.
    sourcemap: true,
  },
});
