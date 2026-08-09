// Extensionless specifiers throughout the web package: this is a bundled app,
// resolved by Vite with `moduleResolution: bundler`, unlike the API which ships
// real Node ESM output and therefore writes `.js` extensions.
export { DEFAULT_API_BASE_URL, FALLBACK_CONFIG, readRuntimeConfig, resolveRuntimeConfig } from './runtime-config';
export type { RuntimeConfig, RuntimeConfigResolution } from './runtime-config';
