"""Subprocess helper for publishctl.

All subprocess calls pass argument lists — never interpolated shell strings — so
user-supplied values cannot inject commands. Exit codes and stderr are surfaced
faithfully rather than swallowed behind a generic error.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Sequence

from rich.console import Console

console = Console(stderr=True)


def run(
    args: Sequence[str],
    *,
    check: bool = True,
    capture: bool = False,
    stream: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with an argument list (no shell interpolation).

    Parameters
    ----------
    args:
        Command and arguments as a list (never a shell string).
    check:
        If True, exit the CLI with the subprocess exit code on failure.
    capture:
        If True, capture stdout/stderr and return them on the result.
    stream:
        If True, stream stdout/stderr to the terminal in real time
        (incompatible with capture).

    Returns
    -------
    subprocess.CompletedProcess with returncode, stdout, stderr.
    """
    cmd_display = " ".join(args)

    try:
        if stream:
            # Stream output live — useful for logs --follow and long builds.
            result = subprocess.run(
                list(args),
                check=False,
                text=True,
            )
        elif capture:
            result = subprocess.run(
                list(args),
                check=False,
                capture_output=True,
                text=True,
            )
        else:
            result = subprocess.run(
                list(args),
                check=False,
                text=True,
            )
    except FileNotFoundError:
        console.print(f"[red]ERROR:[/red] command not found: {args[0]}")
        console.print(f"       full command: {cmd_display}")
        sys.exit(127)

    if check and result.returncode != 0:
        console.print(
            f"[red]ERROR:[/red] command failed (exit {result.returncode}): {cmd_display}"
        )
        if capture and result.stderr:
            console.print(result.stderr.rstrip())
        sys.exit(result.returncode)

    return result


def make(*targets: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a Makefile target from the repository root.

    Parameters
    ----------
    targets:
        One or more make targets.
    check:
        If True, exit on failure.
    capture:
        If True, capture output.
    """
    return run(["make", *targets], check=check, capture=capture)


def kubectl(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run kubectl with the given arguments."""
    return run(["kubectl", *args], check=check, capture=capture)
