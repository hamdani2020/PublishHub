// Runtime configuration placeholder (Requirement 4.7).
//
// Vite copies this file into `dist/` verbatim, and the container entrypoint
// overwrites it at start-up with values taken from the environment (API_BASE_URL
// and friends — spec task 7.2). Shipping it has two benefits: `vite dev` gets a
// working configuration with no extra setup, and the built image always has the
// file the entrypoint expects to replace.
//
// The value below is also the safe default everywhere: nginx proxies /api to the
// API Service, so a same-origin relative base URL works in the container as
// well as behind the dev server proxy. `src/config/runtime-config.ts` applies the
// same default again if this file is missing or malformed.
window.__PUBLISHHUB_CONFIG__ = {
  apiBaseUrl: '/api',
};
