/**
 * Tracer initialization, as a module side effect.
 *
 * This is the one place in the service where a side effect at import time is the
 * correct design rather than a smell. `dd-trace` patches `http`, `express`,
 * `ioredis`, and the AWS SDK when it initializes, and it can only patch a module
 * that has not been evaluated yet. ES modules evaluate their imports in order,
 * depth first, before the importing module's own body runs — so the only way to
 * get in front of `express` is to be the first import of the entrypoint and to do
 * the work during evaluation.
 *
 * Which is why `index.ts` imports this module first, and why nothing else imports
 * it at all: importing it is what starts the tracer. `observability/index.ts`
 * deliberately does not re-export it, so a test that reaches for the module's
 * public surface cannot accidentally load an APM library.
 *
 * With `OBSERVABILITY_ENABLED` unset or false, evaluating this module reads four
 * environment variables and returns {@link INERT_TRACING}. Nothing is loaded,
 * nothing is patched, no socket is opened (Requirement 14.6).
 */

import { loadDatadogTracer } from './dd-trace-loader.js';
import { createTracing, tracingOptionsFromEnv } from './tracing.js';
import type { Tracing } from './tracing.js';

/**
 * The process-wide tracing seam. Inert unless the switch is on.
 *
 * A failed load is reported on stderr and then ignored: there is no logger this
 * early — its fields come from a configuration that has not been parsed yet — and
 * a missing APM library is not a reason to refuse to serve traffic.
 */
export const tracing: Tracing = createTracing(
  {
    ...tracingOptionsFromEnv(process.env),
    onLoadError: (error: unknown) => {
      const detail = error instanceof Error ? error.message : String(error);
      process.stderr.write(`tracing disabled: dd-trace failed to initialize: ${detail}\n`);
    },
  },
  loadDatadogTracer,
);
