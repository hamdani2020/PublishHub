/**
 * Boolean environment flags, in one place.
 *
 * `config.ts` parses them as part of the validated `ApiConfig`, and the tracer
 * bootstrap parses `OBSERVABILITY_ENABLED` on its own — it runs before the
 * configuration module can be imported, because loading that module would pull
 * in the very clients `dd-trace` has to patch first. Two readers, one definition
 * of what counts as true, so the switch cannot mean one thing to the tracer and
 * another to the rest of the service.
 *
 * This file deliberately imports nothing.
 */

export const TRUTHY_FLAG_VALUES: readonly string[] = ['1', 'true', 'yes', 'on'];
export const FALSY_FLAG_VALUES: readonly string[] = ['0', 'false', 'no', 'off'];

/** Human-readable list for a validation message. */
export const BOOLEAN_FLAG_HINT = `must be a boolean: one of ${[
  ...TRUTHY_FLAG_VALUES,
  ...FALSY_FLAG_VALUES,
]
  .filter((value, index, all) => all.indexOf(value) === index)
  .join(', ')}`;

/**
 * `true` or `false` for a recognized value, `null` for anything else — including
 * an absent or blank variable. Callers decide whether an unrecognized value is a
 * startup failure (configuration) or simply "off" (the tracer bootstrap, which
 * has no logger yet and must not crash the process over a typo it cannot report).
 */
export function parseBooleanFlag(value: string | undefined): boolean | null {
  if (value === undefined) {
    return null;
  }
  const normalized = value.trim().toLowerCase();
  if (TRUTHY_FLAG_VALUES.includes(normalized)) {
    return true;
  }
  if (FALSY_FLAG_VALUES.includes(normalized)) {
    return false;
  }
  return null;
}
