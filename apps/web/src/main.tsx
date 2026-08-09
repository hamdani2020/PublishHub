import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
import { readRuntimeConfig } from './config';
import './styles.css';

/**
 * Browser entry point.
 *
 * The runtime configuration is read once, here, and handed down as a prop
 * (Requirement 4.7). If `/config.js` was missing or malformed the resolution
 * falls back to a working same-origin default and reports why, which is logged
 * once rather than swallowed — a silent fallback is how a deployment ends up
 * quietly talking to the wrong API.
 */
const resolution = readRuntimeConfig();
if (resolution.problem !== null) {
  // eslint-disable-next-line no-console -- one boot-time warning; the browser console is the only place a user or operator can see this.
  console.warn(
    `[publishhub] runtime configuration fell back to defaults: ${resolution.problem}. Using apiBaseUrl "${resolution.config.apiBaseUrl}".`,
  );
}

const container = document.getElementById('root');
if (container === null) {
  // index.html always ships the mount point, so this only fires if the served
  // HTML has been replaced. Failing loudly beats rendering nothing.
  throw new Error('Cannot mount PublishHub: no #root element in the document');
}

createRoot(container).render(
  <StrictMode>
    <App config={resolution.config} />
  </StrictMode>,
);
