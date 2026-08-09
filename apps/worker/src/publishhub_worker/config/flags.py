"""
Boolean environment flags, in one place.

Mirrors `apps/api/src/config/flags.ts`, value for value. `OBSERVABILITY_ENABLED`
is read by two things — the configuration loader and, later, the tracer bootstrap
that has to run before anything else is imported — so what counts as true is
defined once instead of twice.

This module deliberately imports nothing.
"""

from __future__ import annotations

TRUTHY_FLAG_VALUES: tuple[str, ...] = ("1", "true", "yes", "on")
FALSY_FLAG_VALUES: tuple[str, ...] = ("0", "false", "no", "off")

#: Human-readable list for a validation message.
BOOLEAN_FLAG_HINT = "must be a boolean: one of " + ", ".join(
    (*TRUTHY_FLAG_VALUES, *FALSY_FLAG_VALUES)
)


def parse_boolean_flag(value: str | None) -> bool | None:
    """
    `True` or `False` for a recognized value, `None` for anything else —
    including an absent or blank variable. The caller decides whether an
    unrecognized value is a startup failure (configuration, which can report the
    offending key) or simply "off" (a bootstrap with no logger yet, which must
    not crash the process over a typo it cannot report).
    """
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in TRUTHY_FLAG_VALUES:
        return True
    if normalized in FALSY_FLAG_VALUES:
        return False
    return None
