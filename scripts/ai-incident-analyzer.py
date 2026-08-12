#!/usr/bin/env python3
"""AI incident analyzer — collects Kubernetes diagnostic data and analyzes with Bedrock.

This module implements:
1. Diagnostic collection from kubectl (pod description, logs, events)
2. Secret redaction before any data leaves the machine
3. Section truncation to documented character budgets
4. Bedrock analysis with structured SRE prompt (task 14.2)
5. Report formatting (task 14.3)

Usage:
    python3 scripts/ai-incident-analyzer.py --pod NAME --namespace NS [--json]
"""

from __future__ import annotations

import argparse
import json as json_module
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

# ─── Character budgets per section ──────────────────────────────────────────────

BUDGET_DESCRIPTION = 3000
BUDGET_LOGS = 4000
BUDGET_PREVIOUS_LOGS = 2000
BUDGET_EVENTS = 2000

# ─── Redaction patterns ─────────────────────────────────────────────────────────

# AWS access key IDs
_RE_AWS_KEY = re.compile(r"AKIA[0-9A-Z]{16}")

# Bearer tokens (Authorization: Bearer ...)
_RE_BEARER = re.compile(r"(?i)(bearer\s+)[^\s\"']+")

# password= values in key=value pairs
_RE_PASSWORD = re.compile(r"(?i)(password\s*=\s*)[^\s\"',;]+")

# Environment variable keys ending in _KEY or _SECRET with their values
_RE_KEY_SECRET_ENV = re.compile(r"(?i)(\w*(?:_KEY|_SECRET)\s*=\s*)[^\s\"',;]+")

# JWT-shaped strings (three dot-separated base64url segments)
_RE_JWT = re.compile(
    r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)

# URLs with embedded credentials (proto://user:pass@host)
_RE_URL_CREDS = re.compile(
    r"(?i)(https?://[^:]+:)[^\s@]+(@)"
)

# Generic secret-looking assignments (e.g. SECRET_KEY=abc123)
_RE_GENERIC_SECRET = re.compile(r"(?i)(\w*secret\w*\s*[:=]\s*)[^\s\"',;]+")

REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_RE_AWS_KEY, "[REDACTED]"),
    (_RE_JWT, "[REDACTED]"),
    (_RE_BEARER, r"\1[REDACTED]"),
    (_RE_PASSWORD, r"\1[REDACTED]"),
    (_RE_KEY_SECRET_ENV, r"\1[REDACTED]"),
    (_RE_URL_CREDS, r"\1[REDACTED]\2"),
    (_RE_GENERIC_SECRET, r"\1[REDACTED]"),
]


# ─── Data structures ───────────────────────────────────────────────────────────


@dataclass
class DiagnosticSection:
    """A single section of collected diagnostic data."""

    title: str
    content: str
    error: str | None = None


@dataclass
class DiagnosticReport:
    """All collected diagnostic data for a pod."""

    pod: str
    namespace: str
    description: DiagnosticSection = field(default_factory=lambda: DiagnosticSection("Pod Description", ""))
    logs: DiagnosticSection = field(default_factory=lambda: DiagnosticSection("Recent Logs", ""))
    previous_logs: DiagnosticSection = field(default_factory=lambda: DiagnosticSection("Previous Container Logs", ""))
    events: DiagnosticSection = field(default_factory=lambda: DiagnosticSection("Related Events", ""))


# ─── Core functions ─────────────────────────────────────────────────────────────


def redact(text: str) -> str:
    """Remove secret-shaped values from text.

    Applies all redaction patterns to the input text, replacing matches
    with [REDACTED] to prevent sensitive data from leaving the machine.
    """
    result = text
    for pattern, replacement in REDACTION_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def truncate(text: str, budget: int) -> str:
    """Truncate text to the given character budget.

    If the text exceeds the budget, it is cut and a truncation notice
    is appended so the consumer knows the content was shortened.
    """
    if len(text) <= budget:
        return text
    marker = "\n... [truncated to {} chars]".format(budget)
    return text[: budget - len(marker)] + marker


def run_kubectl(
    args: Sequence[str],
) -> tuple[str, str | None]:
    """Run a kubectl command, returning (stdout, error_message | None).

    Never raises on kubectl failure — records the failure as an error string
    so the analysis can proceed with partial data.
    """
    cmd = ["kubectl", *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else f"exit code {result.returncode}"
            return "", f"kubectl failed: {error_msg}"
        return result.stdout, None
    except FileNotFoundError:
        return "", "kubectl not found on PATH"
    except subprocess.TimeoutExpired:
        return "", "kubectl timed out after 30s"
    except OSError as exc:
        return "", f"kubectl execution error: {exc}"


def collect_diagnostics(pod: str, namespace: str) -> DiagnosticReport:
    """Collect all diagnostic data for a pod.

    Gathers pod description, recent logs, previous container logs, and
    related events. Individual kubectl failures are recorded in the
    section's error field rather than aborting the collection.
    """
    report = DiagnosticReport(pod=pod, namespace=namespace)

    # Pod description
    stdout, err = run_kubectl(["describe", "pod", pod, "-n", namespace])
    report.description = DiagnosticSection(
        title="Pod Description",
        content=stdout,
        error=err,
    )

    # Recent logs (last 100 lines)
    stdout, err = run_kubectl(["logs", pod, "-n", namespace, "--tail=100"])
    report.logs = DiagnosticSection(
        title="Recent Logs",
        content=stdout,
        error=err,
    )

    # Previous container logs (for restarts)
    stdout, err = run_kubectl(["logs", pod, "-n", namespace, "--previous", "--tail=50"])
    report.previous_logs = DiagnosticSection(
        title="Previous Container Logs",
        content=stdout,
        error=err,
    )

    # Related events
    stdout, err = run_kubectl([
        "get", "events", "-n", namespace,
        "--field-selector", f"involvedObject.name={pod}",
        "--sort-by=.lastTimestamp",
    ])
    report.events = DiagnosticSection(
        title="Related Events",
        content=stdout,
        error=err,
    )

    return report


def prepare_report_text(report: DiagnosticReport) -> str:
    """Redact and truncate all sections, then assemble into a single text block.

    This is the text that would be sent to Bedrock for analysis. Each section
    is individually redacted and truncated to its documented character budget.
    """
    sections: list[tuple[DiagnosticSection, int]] = [
        (report.description, BUDGET_DESCRIPTION),
        (report.logs, BUDGET_LOGS),
        (report.previous_logs, BUDGET_PREVIOUS_LOGS),
        (report.events, BUDGET_EVENTS),
    ]

    parts: list[str] = []
    parts.append(f"Pod: {report.pod}")
    parts.append(f"Namespace: {report.namespace}")
    parts.append("")

    for section, budget in sections:
        parts.append(f"=== {section.title} ===")
        if section.error:
            parts.append(f"[Collection error: {section.error}]")
        if section.content:
            redacted = redact(section.content)
            truncated = truncate(redacted, budget)
            parts.append(truncated)
        elif not section.error:
            parts.append("(no data)")
        parts.append("")

    return "\n".join(parts)


# ─── Bedrock analysis ───────────────────────────────────────────────────────────

BEDROCK_MODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0"

# Maximum number of retries for throttling errors
MAX_RETRIES = 3

# Base delay in seconds for exponential backoff on throttling
RETRY_BASE_DELAY = 1.0

SRE_PROMPT = """\
You are an experienced Site Reliability Engineer performing incident triage on a Kubernetes pod.

Analyze the following diagnostic data and provide a structured report with:
1. **Summary**: A brief one-sentence description of the problem.
2. **Hypotheses**: A ranked list of root-cause hypotheses (most likely first), each with a brief explanation.
3. **Recommended Fix**: The most actionable next step to resolve the issue.
4. **Severity**: One of: critical, high, medium, low.
5. **Category**: One of: CrashLoop, OOM, ImagePull, Configuration, Networking, ResourceLimit, Scheduling, Storage, Other.

Respond in valid JSON with these exact keys: summary, hypotheses (array of strings), fix, severity, category.

--- DIAGNOSTIC DATA ---
{diagnostic_text}
"""


class BedrockAnalysisError(Exception):
    """Raised when Bedrock analysis fails with an actionable message."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def analyze_with_bedrock(report_text: str) -> dict[str, Any]:
    """Send diagnostic text to Bedrock Claude 3 Haiku and return parsed analysis.

    Authenticates through the ambient AWS credential chain. Maps Bedrock
    errors to actionable diagnostics. Retries on throttling with backoff.

    Returns a dict with keys: summary, hypotheses, fix, severity, category.

    Raises:
        BedrockAnalysisError: On non-retryable AWS/Bedrock errors with
            an actionable message for the operator.
    """
    try:
        import boto3
        from botocore.exceptions import (
            ClientError,
            NoCredentialsError,
        )
    except ImportError:
        raise BedrockAnalysisError(
            "boto3 is not installed; install it with: pip install boto3"
        )

    prompt_text = SRE_PROMPT.format(diagnostic_text=report_text)

    # Build the request body for Claude 3 via Bedrock Messages API
    request_body = json_module.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": prompt_text,
            }
        ],
    })

    try:
        client = boto3.client("bedrock-runtime")
    except NoCredentialsError:
        raise BedrockAnalysisError(
            "AWS credentials missing or expired; run `aws configure` or refresh SSO"
        )

    # Retry loop for throttling
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.invoke_model(
                modelId=BEDROCK_MODEL_ID,
                contentType="application/json",
                accept="application/json",
                body=request_body,
            )
            break
        except NoCredentialsError:
            raise BedrockAnalysisError(
                "AWS credentials missing or expired; run `aws configure` or refresh SSO"
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            error_message = exc.response.get("Error", {}).get("Message", "")

            if error_code == "AccessDeniedException":
                raise BedrockAnalysisError(
                    "Model access not granted; enable Claude 3 Haiku in the Bedrock console for this region"
                )
            elif error_code == "ValidationException":
                raise BedrockAnalysisError(
                    "Model unavailable in region; try us-east-1"
                )
            elif error_code in ("ExpiredTokenException",):
                raise BedrockAnalysisError(
                    "AWS credentials missing or expired; run `aws configure` or refresh SSO"
                )
            elif error_code == "ThrottlingException":
                last_error = exc
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    time.sleep(delay)
                    continue
                else:
                    raise BedrockAnalysisError(
                        f"Bedrock throttled after {MAX_RETRIES} retries; try again later"
                    )
            else:
                raise BedrockAnalysisError(
                    f"Bedrock error ({error_code}): {error_message}"
                )
    else:
        # Should not be reached due to the break/raise logic above,
        # but acts as a safety net.
        raise BedrockAnalysisError(
            f"Bedrock request failed after {MAX_RETRIES} attempts"
        )

    # Parse the response
    response_body = json_module.loads(response["body"].read())

    # Claude 3 Messages API returns content as a list of content blocks
    content_blocks = response_body.get("content", [])
    if not content_blocks:
        raise BedrockAnalysisError("Empty response from Bedrock model")

    # Extract text from the first text block
    text_content = ""
    for block in content_blocks:
        if block.get("type") == "text":
            text_content = block.get("text", "")
            break

    if not text_content:
        raise BedrockAnalysisError("No text content in Bedrock response")

    # Parse JSON from the model's response
    # The model may wrap JSON in markdown code fences — strip them
    cleaned = text_content.strip()
    if cleaned.startswith("```"):
        # Remove opening fence (possibly ```json)
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[: -3].rstrip()

    try:
        analysis = json_module.loads(cleaned)
    except json_module.JSONDecodeError:
        raise BedrockAnalysisError(
            f"Failed to parse model response as JSON: {text_content[:200]}"
        )

    # Ensure expected keys exist with defaults
    result: dict[str, Any] = {
        "summary": analysis.get("summary", "No summary provided"),
        "hypotheses": analysis.get("hypotheses", []),
        "fix": analysis.get("fix", "No fix recommendation provided"),
        "severity": analysis.get("severity", "medium"),
        "category": analysis.get("category", "Other"),
    }

    return result


# ─── Report formatting ──────────────────────────────────────────────────────────

SEVERITY_COLORS = {
    "critical": "\033[1;31m",  # bold red
    "high": "\033[31m",        # red
    "medium": "\033[33m",      # yellow
    "low": "\033[32m",         # green
}
COLOR_RESET = "\033[0m"


def _build_json_output(
    report: DiagnosticReport,
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the JSON output dictionary for the incident report.

    Includes pod, namespace, sections (each with redacted+truncated content
    and error), and optionally the Bedrock analysis results.
    """
    output: dict[str, Any] = {
        "pod": report.pod,
        "namespace": report.namespace,
        "sections": {
            "description": {
                "content": redact(truncate(report.description.content, BUDGET_DESCRIPTION)),
                "error": report.description.error,
            },
            "logs": {
                "content": redact(truncate(report.logs.content, BUDGET_LOGS)),
                "error": report.logs.error,
            },
            "previous_logs": {
                "content": redact(truncate(report.previous_logs.content, BUDGET_PREVIOUS_LOGS)),
                "error": report.previous_logs.error,
            },
            "events": {
                "content": redact(truncate(report.events.content, BUDGET_EVENTS)),
                "error": report.events.error,
            },
        },
    }
    if analysis is not None:
        output["analysis"] = analysis
    return output


def format_text_report(
    report_text: str,
    analysis: dict[str, Any],
    *,
    color: bool | None = None,
) -> str:
    """Format the full incident report for terminal display.

    Combines the diagnostic data with the AI analysis into a professional,
    scannable report designed for on-call engineers at 2am.

    Parameters
    ----------
    report_text:
        Pre-assembled diagnostic text (already redacted and truncated).
    analysis:
        Parsed Bedrock analysis dict with summary, hypotheses, fix, severity, category.
    color:
        Force color on/off. If None, auto-detect from sys.stdout.isatty().
    """
    if color is None:
        color = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    severity = analysis.get("severity", "medium").lower()
    category = analysis.get("category", "Other")
    summary = analysis.get("summary", "No summary provided")
    hypotheses = analysis.get("hypotheses", [])
    fix = analysis.get("fix", "No fix recommendation provided")

    # Severity badge with optional color
    if color:
        sev_color = SEVERITY_COLORS.get(severity, "")
        severity_display = f"{sev_color}{severity.upper()}{COLOR_RESET}"
    else:
        severity_display = severity.upper()

    lines: list[str] = []

    # Header banner
    lines.append("")
    lines.append("+" + "-" * 58 + "+")
    lines.append("|" + " INCIDENT ANALYSIS REPORT".center(58) + "|")
    lines.append("+" + "-" * 58 + "+")
    lines.append("")

    # Diagnostic data section
    lines.append(report_text)
    lines.append("")

    # Analysis section
    lines.append("+" + "=" * 58 + "+")
    lines.append("|" + " AI ANALYSIS".center(58) + "|")
    lines.append("+" + "=" * 58 + "+")
    lines.append("")
    lines.append(f"  Severity : {severity_display}")
    lines.append(f"  Category : {category}")
    lines.append("")
    lines.append(f"  Summary  : {summary}")
    lines.append("")

    # Hypotheses
    if hypotheses:
        lines.append("  Root-Cause Hypotheses (ranked):")
        for i, hypothesis in enumerate(hypotheses, 1):
            lines.append(f"    {i}. {hypothesis}")
        lines.append("")

    # Recommended fix
    lines.append(f"  Recommended Fix:")
    lines.append(f"    {fix}")
    lines.append("")
    lines.append("+" + "-" * 58 + "+")

    return "\n".join(lines)


# ─── CLI entry point ────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the incident analyzer.

    Collects diagnostics and optionally analyzes with Bedrock.
    Use --collect-only to skip Bedrock analysis.
    """
    parser = argparse.ArgumentParser(
        description="AI incident analyzer — collects pod diagnostics and analyzes with Bedrock"
    )
    parser.add_argument("--pod", required=True, help="Name of the pod to analyze")
    parser.add_argument("--namespace", default="publishhub", help="Kubernetes namespace")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output in JSON format")
    parser.add_argument(
        "--collect-only",
        action="store_true",
        dest="collect_only",
        help="Only collect diagnostics; skip Bedrock analysis",
    )

    args = parser.parse_args(argv)

    # Collect diagnostics
    report = collect_diagnostics(args.pod, args.namespace)

    # Prepare the redacted and truncated text
    report_text = prepare_report_text(report)

    if args.collect_only:
        # Output diagnostics only, no Bedrock analysis
        if args.json_output:
            output = _build_json_output(report, analysis=None)
            print(json_module.dumps(output, indent=2))
        else:
            print(report_text)
        return 0

    # Analyze with Bedrock
    try:
        analysis = analyze_with_bedrock(report_text)
    except BedrockAnalysisError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return exc.exit_code

    # Output the analysis
    if args.json_output:
        output = _build_json_output(report, analysis=analysis)
        print(json_module.dumps(output, indent=2))
    else:
        print(format_text_report(report_text, analysis))

    return 0


if __name__ == "__main__":
    sys.exit(main())
