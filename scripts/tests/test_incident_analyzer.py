"""Unit tests for the AI incident analyzer diagnostic collection.

Covers:
- Redaction patterns (AWS keys, bearer tokens, passwords, JWT, URL creds, etc.)
- Truncation to documented character budgets
- kubectl failure handling (records error, does not abort)
- End-to-end diagnostic collection with mocked subprocess
- Bedrock analysis with mocked boto3 (success, error mapping, retry)
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

# Import the module using importlib since the filename contains a hyphen.
# We must register it in sys.modules before exec so dataclass introspection works.
_MODULE_NAME = "ai_incident_analyzer"
_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ai-incident-analyzer.py",
)
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = _module
_spec.loader.exec_module(_module)

redact = _module.redact
truncate = _module.truncate
run_kubectl = _module.run_kubectl
collect_diagnostics = _module.collect_diagnostics
prepare_report_text = _module.prepare_report_text
analyze_with_bedrock = _module.analyze_with_bedrock
BedrockAnalysisError = _module.BedrockAnalysisError
main = _module.main
DiagnosticSection = _module.DiagnosticSection
DiagnosticReport = _module.DiagnosticReport
BUDGET_DESCRIPTION = _module.BUDGET_DESCRIPTION
BUDGET_LOGS = _module.BUDGET_LOGS
BUDGET_PREVIOUS_LOGS = _module.BUDGET_PREVIOUS_LOGS
BUDGET_EVENTS = _module.BUDGET_EVENTS
BEDROCK_MODEL_ID = _module.BEDROCK_MODEL_ID
MAX_RETRIES = _module.MAX_RETRIES
format_text_report = _module.format_text_report
_build_json_output = _module._build_json_output


# ─── Redaction tests ────────────────────────────────────────────────────────────


class TestRedaction:
    """Tests for the redact() function covering all documented patterns."""

    def test_aws_access_key_redacted(self) -> None:
        text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        result = redact(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[REDACTED]" in result

    def test_bearer_token_redacted(self) -> None:
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
        result = redact(text)
        # The JWT pattern and bearer pattern both apply
        assert "[REDACTED]" in result
        # The actual token value should not appear
        assert "eyJhbGciOiJIUzI1NiJ9.payload.sig" not in result

    def test_bearer_token_case_insensitive(self) -> None:
        text = "authorization: BEARER my-secret-token-value"
        result = redact(text)
        assert "my-secret-token-value" not in result
        assert "[REDACTED]" in result

    def test_password_value_redacted(self) -> None:
        text = "REDIS_PASSWORD=super_secret_123"
        result = redact(text)
        assert "super_secret_123" not in result
        assert "[REDACTED]" in result

    def test_password_with_equals_space(self) -> None:
        text = "password = my_db_pass"
        result = redact(text)
        assert "my_db_pass" not in result
        assert "[REDACTED]" in result

    def test_env_key_suffix_redacted(self) -> None:
        text = "DB_SECRET_KEY=abc123def456"
        result = redact(text)
        assert "abc123def456" not in result
        assert "[REDACTED]" in result

    def test_env_secret_suffix_redacted(self) -> None:
        text = "API_SECRET=xyzzy"
        result = redact(text)
        assert "xyzzy" not in result
        assert "[REDACTED]" in result

    def test_jwt_shaped_string_redacted(self) -> None:
        # A plausible JWT: header.payload.signature (each base64url)
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        text = f"token: {jwt}"
        result = redact(text)
        assert jwt not in result
        assert "[REDACTED]" in result

    def test_url_with_credentials_redacted(self) -> None:
        text = "connecting to http://admin:p4ssw0rd@db.example.com:5432/mydb"
        result = redact(text)
        assert "p4ssw0rd" not in result
        assert "[REDACTED]" in result
        # The host should still be visible
        assert "db.example.com" in result

    def test_https_url_with_credentials_redacted(self) -> None:
        text = "REDIS_URL=https://user:secret123@redis.internal:6379"
        result = redact(text)
        assert "secret123" not in result
        assert "[REDACTED]" in result

    def test_multiple_patterns_in_one_text(self) -> None:
        text = (
            "AWS_KEY=AKIAIOSFODNN7EXAMPLE\n"
            "DB_PASSWORD=hunter2\n"
            "Authorization: Bearer abc123token\n"
        )
        result = redact(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "hunter2" not in result
        assert "abc123token" not in result

    def test_safe_text_unchanged(self) -> None:
        text = "Pod publishhub-api-5d4f8b6c7-x2k9m is Running\nReady: True"
        result = redact(text)
        assert result == text

    def test_empty_string(self) -> None:
        assert redact("") == ""

    def test_generic_secret_assignment(self) -> None:
        text = "MY_APP_SECRET: very-private-value"
        result = redact(text)
        assert "very-private-value" not in result
        assert "[REDACTED]" in result


# ─── Truncation tests ───────────────────────────────────────────────────────────


class TestTruncation:
    """Tests for the truncate() function with documented character budgets."""

    def test_short_text_unchanged(self) -> None:
        text = "short text"
        assert truncate(text, 100) == text

    def test_text_at_exact_budget_unchanged(self) -> None:
        text = "x" * 3000
        assert truncate(text, 3000) == text

    def test_text_exceeding_budget_is_cut(self) -> None:
        text = "a" * 5000
        result = truncate(text, 3000)
        assert len(result) <= 3000
        assert "[truncated to 3000 chars]" in result

    def test_truncated_text_ends_with_marker(self) -> None:
        text = "b" * 10000
        result = truncate(text, 4000)
        assert result.endswith("[truncated to 4000 chars]")

    def test_budget_description(self) -> None:
        text = "d" * (BUDGET_DESCRIPTION + 500)
        result = truncate(text, BUDGET_DESCRIPTION)
        assert len(result) <= BUDGET_DESCRIPTION

    def test_budget_logs(self) -> None:
        text = "l" * (BUDGET_LOGS + 500)
        result = truncate(text, BUDGET_LOGS)
        assert len(result) <= BUDGET_LOGS

    def test_budget_previous_logs(self) -> None:
        text = "p" * (BUDGET_PREVIOUS_LOGS + 500)
        result = truncate(text, BUDGET_PREVIOUS_LOGS)
        assert len(result) <= BUDGET_PREVIOUS_LOGS

    def test_budget_events(self) -> None:
        text = "e" * (BUDGET_EVENTS + 500)
        result = truncate(text, BUDGET_EVENTS)
        assert len(result) <= BUDGET_EVENTS

    def test_empty_string(self) -> None:
        assert truncate("", 100) == ""


# ─── kubectl failure handling ───────────────────────────────────────────────────


class TestRunKubectl:
    """Tests for run_kubectl() graceful failure handling."""

    @patch("subprocess.run")
    def test_successful_command(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["kubectl", "describe", "pod", "test-pod"],
            returncode=0,
            stdout="Name: test-pod\nStatus: Running",
            stderr="",
        )
        stdout, err = run_kubectl(["describe", "pod", "test-pod"])
        assert stdout == "Name: test-pod\nStatus: Running"
        assert err is None

    @patch("subprocess.run")
    def test_failed_command_returns_error(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["kubectl", "logs", "missing-pod"],
            returncode=1,
            stdout="",
            stderr="Error from server (NotFound): pods \"missing-pod\" not found",
        )
        stdout, err = run_kubectl(["logs", "missing-pod"])
        assert stdout == ""
        assert err is not None
        assert "NotFound" in err

    @patch("subprocess.run")
    def test_command_not_found(self, mock_run) -> None:
        mock_run.side_effect = FileNotFoundError("No such file")
        stdout, err = run_kubectl(["get", "pods"])
        assert stdout == ""
        assert err == "kubectl not found on PATH"

    @patch("subprocess.run")
    def test_command_timeout(self, mock_run) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="kubectl", timeout=30)
        stdout, err = run_kubectl(["logs", "stuck-pod", "--follow"])
        assert stdout == ""
        assert "timed out" in err

    @patch("subprocess.run")
    def test_os_error(self, mock_run) -> None:
        mock_run.side_effect = OSError("Permission denied")
        stdout, err = run_kubectl(["get", "secrets"])
        assert stdout == ""
        assert "Permission denied" in err

    @patch("subprocess.run")
    def test_empty_stderr_on_failure(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["kubectl", "get", "pod"],
            returncode=1,
            stdout="",
            stderr="",
        )
        stdout, err = run_kubectl(["get", "pod"])
        assert err is not None
        assert "exit code 1" in err


# ─── Diagnostic collection tests ────────────────────────────────────────────────


class TestCollectDiagnostics:
    """Tests for collect_diagnostics() with mocked kubectl calls."""

    @patch("subprocess.run")
    def test_collects_all_sections(self, mock_run) -> None:
        """All four kubectl calls succeed — all sections populated."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["kubectl"],
            returncode=0,
            stdout="mock output",
            stderr="",
        )
        report = collect_diagnostics("my-pod", "publishhub")
        assert report.pod == "my-pod"
        assert report.namespace == "publishhub"
        assert report.description.content == "mock output"
        assert report.logs.content == "mock output"
        assert report.previous_logs.content == "mock output"
        assert report.events.content == "mock output"
        assert report.description.error is None
        assert report.logs.error is None

    @patch("subprocess.run")
    def test_partial_failure_does_not_abort(self, mock_run) -> None:
        """One kubectl call fails, others succeed — collection continues."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 3:
                # Third call (previous logs) fails
                return subprocess.CompletedProcess(
                    args=args[0], returncode=1, stdout="", stderr="previous container not found"
                )
            return subprocess.CompletedProcess(
                args=args[0], returncode=0, stdout="data", stderr=""
            )

        mock_run.side_effect = side_effect
        report = collect_diagnostics("my-pod", "default")

        # Description and logs should have data
        assert report.description.content == "data"
        assert report.logs.content == "data"
        # Previous logs should have the error recorded
        assert report.previous_logs.error is not None
        assert "previous container" in report.previous_logs.error
        # Events should still work
        assert report.events.content == "data"

    @patch("subprocess.run")
    def test_all_failures_recorded(self, mock_run) -> None:
        """All kubectl calls fail — all errors recorded, no exception raised."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["kubectl"], returncode=1, stdout="", stderr="connection refused"
        )
        report = collect_diagnostics("broken-pod", "test-ns")
        assert report.description.error is not None
        assert report.logs.error is not None
        assert report.previous_logs.error is not None
        assert report.events.error is not None


# ─── Report preparation tests ───────────────────────────────────────────────────


class TestPrepareReportText:
    """Tests for prepare_report_text() — redaction + truncation applied."""

    def test_report_includes_pod_info(self) -> None:
        report = DiagnosticReport(pod="api-pod", namespace="publishhub")
        report.description = DiagnosticSection("Pod Description", "running fine")
        text = prepare_report_text(report)
        assert "Pod: api-pod" in text
        assert "Namespace: publishhub" in text

    def test_report_includes_section_headers(self) -> None:
        report = DiagnosticReport(pod="p", namespace="ns")
        report.description = DiagnosticSection("Pod Description", "desc content")
        report.logs = DiagnosticSection("Recent Logs", "log content")
        report.previous_logs = DiagnosticSection("Previous Container Logs", "prev content")
        report.events = DiagnosticSection("Related Events", "event content")
        text = prepare_report_text(report)
        assert "=== Pod Description ===" in text
        assert "=== Recent Logs ===" in text
        assert "=== Previous Container Logs ===" in text
        assert "=== Related Events ===" in text

    def test_report_redacts_secrets_in_content(self) -> None:
        report = DiagnosticReport(pod="p", namespace="ns")
        report.description = DiagnosticSection(
            "Pod Description",
            "DB_PASSWORD=super_secret\nStatus: Running",
        )
        text = prepare_report_text(report)
        assert "super_secret" not in text
        assert "[REDACTED]" in text
        assert "Status: Running" in text

    def test_report_truncates_long_sections(self) -> None:
        report = DiagnosticReport(pod="p", namespace="ns")
        report.logs = DiagnosticSection("Recent Logs", "x" * 10000)
        text = prepare_report_text(report)
        # The logs section should be truncated
        assert f"[truncated to {BUDGET_LOGS} chars]" in text

    def test_report_includes_collection_errors(self) -> None:
        report = DiagnosticReport(pod="p", namespace="ns")
        report.previous_logs = DiagnosticSection(
            "Previous Container Logs", "", error="kubectl failed: container not found"
        )
        text = prepare_report_text(report)
        assert "[Collection error: kubectl failed: container not found]" in text

    def test_report_handles_empty_section(self) -> None:
        report = DiagnosticReport(pod="p", namespace="ns")
        report.events = DiagnosticSection("Related Events", "")
        text = prepare_report_text(report)
        assert "(no data)" in text


# ─── Bedrock analysis tests ─────────────────────────────────────────────────────


def _make_bedrock_response(analysis_dict: dict) -> dict:
    """Helper: build a mock Bedrock response body with the given analysis JSON."""
    response_text = json.dumps(analysis_dict)
    body = io.BytesIO(json.dumps({
        "content": [{"type": "text", "text": response_text}],
        "stop_reason": "end_turn",
    }).encode())
    return {"body": body}


def _make_client_error(code: str, message: str = "error") -> Exception:
    """Helper: build a botocore ClientError with the given error code."""
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "InvokeModel",
    )


class TestAnalyzeWithBedrock:
    """Tests for analyze_with_bedrock() success and error paths."""

    @patch("boto3.client")
    def test_successful_analysis(self, mock_boto_client) -> None:
        """Bedrock returns valid JSON — parsed correctly."""
        analysis = {
            "summary": "Pod is crash-looping due to OOM",
            "hypotheses": [
                "Memory limit too low for workload",
                "Memory leak in application",
            ],
            "fix": "Increase memory limit to 512Mi",
            "severity": "high",
            "category": "OOM",
        }
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _make_bedrock_response(analysis)
        mock_boto_client.return_value = mock_client

        result = analyze_with_bedrock("some diagnostic text")

        assert result["summary"] == "Pod is crash-looping due to OOM"
        assert result["hypotheses"] == [
            "Memory limit too low for workload",
            "Memory leak in application",
        ]
        assert result["fix"] == "Increase memory limit to 512Mi"
        assert result["severity"] == "high"
        assert result["category"] == "OOM"

        # Verify the correct model was called
        mock_client.invoke_model.assert_called_once()
        call_kwargs = mock_client.invoke_model.call_args[1]
        assert call_kwargs["modelId"] == BEDROCK_MODEL_ID

    @patch("boto3.client")
    def test_response_with_markdown_fences(self, mock_boto_client) -> None:
        """Bedrock returns JSON wrapped in markdown code fences."""
        analysis = {"summary": "test", "hypotheses": [], "fix": "fix", "severity": "low", "category": "Other"}
        response_text = f"```json\n{json.dumps(analysis)}\n```"
        body = io.BytesIO(json.dumps({
            "content": [{"type": "text", "text": response_text}],
            "stop_reason": "end_turn",
        }).encode())
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = {"body": body}
        mock_boto_client.return_value = mock_client

        result = analyze_with_bedrock("diagnostic text")
        assert result["summary"] == "test"

    @patch("boto3.client")
    def test_access_denied_error(self, mock_boto_client) -> None:
        """AccessDeniedException maps to model access message."""
        mock_client = MagicMock()
        mock_client.invoke_model.side_effect = _make_client_error("AccessDeniedException")
        mock_boto_client.return_value = mock_client

        with pytest.raises(BedrockAnalysisError) as exc_info:
            analyze_with_bedrock("text")

        assert "Model access not granted" in str(exc_info.value)
        assert "Bedrock console" in str(exc_info.value)

    @patch("boto3.client")
    def test_validation_exception_error(self, mock_boto_client) -> None:
        """ValidationException maps to region suggestion."""
        mock_client = MagicMock()
        mock_client.invoke_model.side_effect = _make_client_error(
            "ValidationException", "Could not resolve the foundation model"
        )
        mock_boto_client.return_value = mock_client

        with pytest.raises(BedrockAnalysisError) as exc_info:
            analyze_with_bedrock("text")

        assert "unavailable in region" in str(exc_info.value)
        assert "us-east-1" in str(exc_info.value)

    @patch("boto3.client")
    def test_expired_token_error(self, mock_boto_client) -> None:
        """ExpiredTokenException maps to credentials message."""
        mock_client = MagicMock()
        mock_client.invoke_model.side_effect = _make_client_error("ExpiredTokenException")
        mock_boto_client.return_value = mock_client

        with pytest.raises(BedrockAnalysisError) as exc_info:
            analyze_with_bedrock("text")

        assert "credentials missing or expired" in str(exc_info.value)
        assert "aws configure" in str(exc_info.value)

    @patch("boto3.client")
    def test_no_credentials_error_on_client_creation(self, mock_boto_client) -> None:
        """NoCredentialsError during client creation maps to credentials message."""
        from botocore.exceptions import NoCredentialsError

        mock_boto_client.side_effect = NoCredentialsError()

        with pytest.raises(BedrockAnalysisError) as exc_info:
            analyze_with_bedrock("text")

        assert "credentials missing or expired" in str(exc_info.value)

    @patch("boto3.client")
    @patch("time.sleep")
    def test_throttling_retries_and_succeeds(self, mock_sleep, mock_boto_client) -> None:
        """ThrottlingException triggers retries; succeeds on second attempt."""
        analysis = {"summary": "ok", "hypotheses": [], "fix": "none", "severity": "low", "category": "Other"}
        mock_client = MagicMock()
        mock_client.invoke_model.side_effect = [
            _make_client_error("ThrottlingException"),
            _make_bedrock_response(analysis),
        ]
        mock_boto_client.return_value = mock_client

        result = analyze_with_bedrock("text")
        assert result["summary"] == "ok"
        # Verify sleep was called for backoff
        mock_sleep.assert_called_once()

    @patch("boto3.client")
    @patch("time.sleep")
    def test_throttling_exhausts_retries(self, mock_sleep, mock_boto_client) -> None:
        """ThrottlingException after all retries raises with retry count."""
        mock_client = MagicMock()
        mock_client.invoke_model.side_effect = [
            _make_client_error("ThrottlingException"),
            _make_client_error("ThrottlingException"),
            _make_client_error("ThrottlingException"),
        ]
        mock_boto_client.return_value = mock_client

        with pytest.raises(BedrockAnalysisError) as exc_info:
            analyze_with_bedrock("text")

        assert "throttled" in str(exc_info.value)
        assert str(MAX_RETRIES) in str(exc_info.value)

    @patch("boto3.client")
    def test_unknown_client_error(self, mock_boto_client) -> None:
        """Unknown ClientError surfaces the error code and message."""
        mock_client = MagicMock()
        mock_client.invoke_model.side_effect = _make_client_error(
            "ServiceUnavailableException", "Service is down"
        )
        mock_boto_client.return_value = mock_client

        with pytest.raises(BedrockAnalysisError) as exc_info:
            analyze_with_bedrock("text")

        assert "ServiceUnavailableException" in str(exc_info.value)
        assert "Service is down" in str(exc_info.value)

    @patch("boto3.client")
    def test_empty_response_content(self, mock_boto_client) -> None:
        """Empty content blocks raises a clear error."""
        body = io.BytesIO(json.dumps({
            "content": [],
            "stop_reason": "end_turn",
        }).encode())
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = {"body": body}
        mock_boto_client.return_value = mock_client

        with pytest.raises(BedrockAnalysisError) as exc_info:
            analyze_with_bedrock("text")

        assert "Empty response" in str(exc_info.value)

    @patch("boto3.client")
    def test_invalid_json_in_response(self, mock_boto_client) -> None:
        """Non-JSON text in response raises a parse error."""
        body = io.BytesIO(json.dumps({
            "content": [{"type": "text", "text": "This is not JSON at all"}],
            "stop_reason": "end_turn",
        }).encode())
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = {"body": body}
        mock_boto_client.return_value = mock_client

        with pytest.raises(BedrockAnalysisError) as exc_info:
            analyze_with_bedrock("text")

        assert "Failed to parse" in str(exc_info.value)

    @patch("boto3.client")
    def test_missing_keys_get_defaults(self, mock_boto_client) -> None:
        """Response with partial keys still returns defaults for missing ones."""
        analysis = {"summary": "partial"}  # Missing hypotheses, fix, severity, category
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _make_bedrock_response(analysis)
        mock_boto_client.return_value = mock_client

        result = analyze_with_bedrock("text")
        assert result["summary"] == "partial"
        assert result["hypotheses"] == []
        assert result["fix"] == "No fix recommendation provided"
        assert result["severity"] == "medium"
        assert result["category"] == "Other"


# ─── CLI integration tests for Bedrock ──────────────────────────────────────────


class TestMainWithBedrock:
    """Tests for the main() function with Bedrock integration."""

    @patch("subprocess.run")
    @patch("boto3.client")
    def test_main_with_analysis(self, mock_boto_client, mock_subprocess, capsys) -> None:
        """main() calls Bedrock and prints analysis when not --collect-only."""
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=["kubectl"], returncode=0, stdout="mock data", stderr=""
        )
        analysis = {
            "summary": "OOM kill",
            "hypotheses": ["Memory too low"],
            "fix": "Bump limits",
            "severity": "high",
            "category": "OOM",
        }
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _make_bedrock_response(analysis)
        mock_boto_client.return_value = mock_client

        exit_code = main(["--pod", "test-pod"])
        assert exit_code == 0

        captured = capsys.readouterr()
        assert "OOM kill" in captured.out
        assert "Memory too low" in captured.out
        assert "Bump limits" in captured.out

    @patch("subprocess.run")
    @patch("boto3.client")
    def test_main_json_with_analysis(self, mock_boto_client, mock_subprocess, capsys) -> None:
        """main() with --json includes analysis in JSON output."""
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=["kubectl"], returncode=0, stdout="mock data", stderr=""
        )
        analysis = {
            "summary": "CrashLoop",
            "hypotheses": ["Config error"],
            "fix": "Fix config",
            "severity": "critical",
            "category": "CrashLoop",
        }
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _make_bedrock_response(analysis)
        mock_boto_client.return_value = mock_client

        exit_code = main(["--pod", "test-pod", "--json"])
        assert exit_code == 0

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["analysis"]["summary"] == "CrashLoop"
        assert output["analysis"]["severity"] == "critical"

    @patch("subprocess.run")
    @patch("boto3.client")
    def test_main_bedrock_error_exits_nonzero(self, mock_boto_client, mock_subprocess, capsys) -> None:
        """main() exits non-zero when Bedrock returns an error."""
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=["kubectl"], returncode=0, stdout="mock data", stderr=""
        )
        mock_client = MagicMock()
        mock_client.invoke_model.side_effect = _make_client_error("AccessDeniedException")
        mock_boto_client.return_value = mock_client

        exit_code = main(["--pod", "test-pod"])
        assert exit_code == 1

        captured = capsys.readouterr()
        assert "Model access not granted" in captured.err

    @patch("subprocess.run")
    def test_main_collect_only_skips_bedrock(self, mock_subprocess, capsys) -> None:
        """main() with --collect-only skips Bedrock entirely."""
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=["kubectl"], returncode=0, stdout="pod running", stderr=""
        )

        exit_code = main(["--pod", "test-pod", "--collect-only"])
        assert exit_code == 0

        captured = capsys.readouterr()
        assert "pod running" in captured.out
        # No AI ANALYSIS section since we skipped Bedrock
        assert "AI ANALYSIS" not in captured.out

    @patch("subprocess.run")
    def test_main_collect_only_json(self, mock_subprocess, capsys) -> None:
        """main() with --collect-only --json outputs diagnostics without analysis."""
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=["kubectl"], returncode=0, stdout="data", stderr=""
        )

        exit_code = main(["--pod", "p", "--namespace", "ns", "--collect-only", "--json"])
        assert exit_code == 0

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "analysis" not in output
        assert output["pod"] == "p"
        assert output["namespace"] == "ns"


# ─── Report formatting tests ────────────────────────────────────────────────────


class TestFormatTextReport:
    """Tests for format_text_report() — professional terminal output."""

    def test_includes_header_banner(self) -> None:
        report_text = "Pod: test-pod\nNamespace: default"
        analysis = {
            "summary": "Pod is OOM killed",
            "hypotheses": ["Memory limit too low"],
            "fix": "Increase memory",
            "severity": "high",
            "category": "OOM",
        }
        result = format_text_report(report_text, analysis, color=False)
        assert "INCIDENT ANALYSIS REPORT" in result

    def test_includes_analysis_section(self) -> None:
        report_text = "Pod: p\nNamespace: ns"
        analysis = {
            "summary": "CrashLoop due to bad config",
            "hypotheses": ["Missing env var", "Wrong image tag"],
            "fix": "Set DATABASE_URL",
            "severity": "critical",
            "category": "CrashLoop",
        }
        result = format_text_report(report_text, analysis, color=False)
        assert "AI ANALYSIS" in result
        assert "CRITICAL" in result
        assert "CrashLoop" in result
        assert "CrashLoop due to bad config" in result
        assert "1. Missing env var" in result
        assert "2. Wrong image tag" in result
        assert "Set DATABASE_URL" in result

    def test_severity_displayed_uppercase(self) -> None:
        analysis = {
            "summary": "test",
            "hypotheses": [],
            "fix": "fix",
            "severity": "medium",
            "category": "Other",
        }
        result = format_text_report("", analysis, color=False)
        assert "MEDIUM" in result

    def test_color_disabled_no_ansi_codes(self) -> None:
        analysis = {
            "summary": "test",
            "hypotheses": [],
            "fix": "fix",
            "severity": "critical",
            "category": "Other",
        }
        result = format_text_report("", analysis, color=False)
        assert "\033[" not in result

    def test_color_enabled_has_ansi_codes(self) -> None:
        analysis = {
            "summary": "test",
            "hypotheses": [],
            "fix": "fix",
            "severity": "critical",
            "category": "Other",
        }
        result = format_text_report("", analysis, color=True)
        assert "\033[1;31m" in result  # bold red for critical
        assert "\033[0m" in result  # reset

    def test_empty_hypotheses_no_section(self) -> None:
        analysis = {
            "summary": "test",
            "hypotheses": [],
            "fix": "fix",
            "severity": "low",
            "category": "Other",
        }
        result = format_text_report("", analysis, color=False)
        assert "Root-Cause Hypotheses" not in result

    def test_diagnostic_text_included(self) -> None:
        report_text = "Pod: my-pod\nNamespace: publishhub\n\n=== Pod Description ===\nRunning"
        analysis = {
            "summary": "ok",
            "hypotheses": [],
            "fix": "none",
            "severity": "low",
            "category": "Other",
        }
        result = format_text_report(report_text, analysis, color=False)
        assert "Pod: my-pod" in result
        assert "=== Pod Description ===" in result


# ─── JSON output builder tests ──────────────────────────────────────────────────


class TestBuildJsonOutput:
    """Tests for _build_json_output() helper."""

    def test_without_analysis(self) -> None:
        report = DiagnosticReport(pod="test-pod", namespace="ns")
        report.description = DiagnosticSection("Pod Description", "desc data")
        report.logs = DiagnosticSection("Recent Logs", "log data")
        report.previous_logs = DiagnosticSection("Previous Container Logs", "")
        report.events = DiagnosticSection("Related Events", "events data")

        output = _build_json_output(report, analysis=None)
        assert output["pod"] == "test-pod"
        assert output["namespace"] == "ns"
        assert "analysis" not in output
        assert output["sections"]["description"]["content"] == "desc data"
        assert output["sections"]["logs"]["content"] == "log data"

    def test_with_analysis(self) -> None:
        report = DiagnosticReport(pod="p", namespace="publishhub")
        analysis = {
            "summary": "OOM",
            "hypotheses": ["low limit"],
            "fix": "bump",
            "severity": "high",
            "category": "OOM",
        }
        output = _build_json_output(report, analysis=analysis)
        assert output["analysis"] == analysis
        assert output["pod"] == "p"

    def test_redacts_secrets_in_json_output(self) -> None:
        report = DiagnosticReport(pod="p", namespace="ns")
        report.description = DiagnosticSection(
            "Pod Description", "DB_PASSWORD=secret123"
        )
        output = _build_json_output(report)
        assert "secret123" not in output["sections"]["description"]["content"]
        assert "[REDACTED]" in output["sections"]["description"]["content"]

    def test_includes_section_errors(self) -> None:
        report = DiagnosticReport(pod="p", namespace="ns")
        report.previous_logs = DiagnosticSection(
            "Previous Container Logs", "", error="container not found"
        )
        output = _build_json_output(report)
        assert output["sections"]["previous_logs"]["error"] == "container not found"


# ─── End-to-end crashed pod simulation ─────────────────────────────────────────


# Realistic kubectl output for a CrashLoopBackOff pod
_DESCRIBE_OUTPUT = """\
Name:         publishhub-api-7f8d9c6b5-x2k9m
Namespace:    publishhub
Priority:     0
Node:         publishhub-cluster-worker/172.18.0.3
Start Time:   Mon, 07 Aug 2026 10:00:00 +0000
Labels:       app.kubernetes.io/name=publishhub-api
              pod-template-hash=7f8d9c6b5
Status:       Running
Containers:
  api:
    Image:         localhost:5001/publishhub-api:abc1234
    State:         Waiting
      Reason:      CrashLoopBackOff
    Last State:    Terminated
      Reason:      OOMKilled
      Exit Code:   137
    Ready:         False
    Restart Count: 5
    Limits:
      memory:  128Mi
    Requests:
      memory:  128Mi
    Environment:
      REDIS_URL:         redis://publishhub-redis:6379
      DB_PASSWORD=super_secret_value
      API_SECRET_KEY=my-api-key-12345
"""

_LOGS_OUTPUT = """\
{"level":"info","msg":"Starting API server","port":3000}
{"level":"info","msg":"Connected to Redis"}
{"level":"error","msg":"Memory allocation failed","heap_used":"127Mi","heap_limit":"128Mi"}
{"level":"fatal","msg":"Process killed by OOM","signal":"SIGKILL"}
"""

_PREVIOUS_LOGS_OUTPUT = """\
{"level":"info","msg":"Starting API server","port":3000}
{"level":"warn","msg":"High memory usage","heap_used":"120Mi"}
{"level":"fatal","msg":"Process killed by OOM","signal":"SIGKILL"}
"""

_EVENTS_OUTPUT = """\
LAST SEEN   TYPE      REASON      OBJECT                                MESSAGE
2m          Normal    Scheduled   pod/publishhub-api-7f8d9c6b5-x2k9m   Successfully assigned
2m          Normal    Pulled      pod/publishhub-api-7f8d9c6b5-x2k9m   Container image pulled
1m          Warning   OOMKilling  pod/publishhub-api-7f8d9c6b5-x2k9m   Memory cgroup out of memory
30s         Warning   BackOff     pod/publishhub-api-7f8d9c6b5-x2k9m   Back-off restarting failed container
"""


class TestEndToEndCrashedPod:
    """Simulate a CrashLoopBackOff/OOM pod and verify the full pipeline."""

    @patch("subprocess.run")
    @patch("boto3.client")
    def test_full_pipeline_text_output(self, mock_boto_client, mock_subprocess, capsys) -> None:
        """Full pipeline: collect → redact → truncate → analyze → format text report."""
        call_index = [0]
        kubectl_outputs = [_DESCRIBE_OUTPUT, _LOGS_OUTPUT, _PREVIOUS_LOGS_OUTPUT, _EVENTS_OUTPUT]

        def subprocess_side_effect(*args, **kwargs):
            cmd = args[0]
            idx = call_index[0]
            call_index[0] += 1
            stdout = kubectl_outputs[idx] if idx < len(kubectl_outputs) else ""
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

        mock_subprocess.side_effect = subprocess_side_effect

        analysis = {
            "summary": "Pod is OOM-killed due to memory limit of 128Mi being too low for the workload",
            "hypotheses": [
                "Memory limit 128Mi is insufficient for the API server heap",
                "Memory leak causing gradual heap growth",
                "Sudden traffic spike increasing memory demand",
            ],
            "fix": "Increase the API pod memory limit to 256Mi or 512Mi in values.yaml",
            "severity": "high",
            "category": "OOM",
        }
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _make_bedrock_response(analysis)
        mock_boto_client.return_value = mock_client

        exit_code = main(["--pod", "publishhub-api-7f8d9c6b5-x2k9m", "--namespace", "publishhub"])
        assert exit_code == 0

        captured = capsys.readouterr()

        # Verify the formatted report structure
        assert "INCIDENT ANALYSIS REPORT" in captured.out
        assert "AI ANALYSIS" in captured.out

        # Verify analysis content is present
        assert "OOM-killed" in captured.out
        assert "128Mi" in captured.out
        assert "Memory limit" in captured.out or "memory limit" in captured.out
        assert "HIGH" in captured.out
        assert "OOM" in captured.out

        # Verify secrets are redacted
        assert "super_secret_value" not in captured.out
        assert "my-api-key-12345" not in captured.out
        assert "[REDACTED]" in captured.out

        # Verify diagnostic data is included
        assert "publishhub-api-7f8d9c6b5-x2k9m" in captured.out
        assert "CrashLoopBackOff" in captured.out

    @patch("subprocess.run")
    @patch("boto3.client")
    def test_full_pipeline_json_output(self, mock_boto_client, mock_subprocess, capsys) -> None:
        """Full pipeline with --json: structured output with all fields."""
        call_index = [0]
        kubectl_outputs = [_DESCRIBE_OUTPUT, _LOGS_OUTPUT, _PREVIOUS_LOGS_OUTPUT, _EVENTS_OUTPUT]

        def subprocess_side_effect(*args, **kwargs):
            cmd = args[0]
            idx = call_index[0]
            call_index[0] += 1
            stdout = kubectl_outputs[idx] if idx < len(kubectl_outputs) else ""
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

        mock_subprocess.side_effect = subprocess_side_effect

        analysis = {
            "summary": "OOM kill",
            "hypotheses": ["Memory too low"],
            "fix": "Increase limits",
            "severity": "high",
            "category": "OOM",
        }
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _make_bedrock_response(analysis)
        mock_boto_client.return_value = mock_client

        exit_code = main(["--pod", "publishhub-api-7f8d9c6b5-x2k9m", "--namespace", "publishhub", "--json"])
        assert exit_code == 0

        captured = capsys.readouterr()
        output = json.loads(captured.out)

        # Verify JSON structure
        assert output["pod"] == "publishhub-api-7f8d9c6b5-x2k9m"
        assert output["namespace"] == "publishhub"

        # Verify analysis is present
        assert output["analysis"]["summary"] == "OOM kill"
        assert output["analysis"]["severity"] == "high"
        assert output["analysis"]["category"] == "OOM"
        assert output["analysis"]["hypotheses"] == ["Memory too low"]
        assert output["analysis"]["fix"] == "Increase limits"

        # Verify sections are present
        assert "description" in output["sections"]
        assert "logs" in output["sections"]
        assert "previous_logs" in output["sections"]
        assert "events" in output["sections"]

        # Verify secrets are redacted in JSON
        assert "super_secret_value" not in output["sections"]["description"]["content"]
        assert "my-api-key-12345" not in output["sections"]["description"]["content"]
        assert "[REDACTED]" in output["sections"]["description"]["content"]

        # Verify diagnostic content is present
        assert "CrashLoopBackOff" in output["sections"]["description"]["content"]
        assert "OOMKilled" in output["sections"]["description"]["content"]
        assert "OOMKilling" in output["sections"]["events"]["content"]

    @patch("subprocess.run")
    def test_collect_only_no_bedrock(self, mock_subprocess, capsys) -> None:
        """--collect-only outputs diagnostics without calling Bedrock."""
        call_index = [0]
        kubectl_outputs = [_DESCRIBE_OUTPUT, _LOGS_OUTPUT, _PREVIOUS_LOGS_OUTPUT, _EVENTS_OUTPUT]

        def subprocess_side_effect(*args, **kwargs):
            cmd = args[0]
            idx = call_index[0]
            call_index[0] += 1
            stdout = kubectl_outputs[idx] if idx < len(kubectl_outputs) else ""
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

        mock_subprocess.side_effect = subprocess_side_effect

        exit_code = main(["--pod", "publishhub-api-7f8d9c6b5-x2k9m", "--collect-only"])
        assert exit_code == 0

        captured = capsys.readouterr()
        # Diagnostics present
        assert "CrashLoopBackOff" in captured.out
        assert "OOMKilled" in captured.out
        # Secrets redacted
        assert "super_secret_value" not in captured.out
        # No AI analysis section
        assert "AI ANALYSIS" not in captured.out

    @patch("subprocess.run")
    def test_collect_only_json_no_analysis_key(self, mock_subprocess, capsys) -> None:
        """--collect-only --json outputs JSON without the 'analysis' key."""
        call_index = [0]
        kubectl_outputs = [_DESCRIBE_OUTPUT, _LOGS_OUTPUT, _PREVIOUS_LOGS_OUTPUT, _EVENTS_OUTPUT]

        def subprocess_side_effect(*args, **kwargs):
            cmd = args[0]
            idx = call_index[0]
            call_index[0] += 1
            stdout = kubectl_outputs[idx] if idx < len(kubectl_outputs) else ""
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

        mock_subprocess.side_effect = subprocess_side_effect

        exit_code = main([
            "--pod", "publishhub-api-7f8d9c6b5-x2k9m",
            "--namespace", "publishhub",
            "--collect-only",
            "--json",
        ])
        assert exit_code == 0

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "analysis" not in output
        assert output["pod"] == "publishhub-api-7f8d9c6b5-x2k9m"
        assert output["namespace"] == "publishhub"
        assert "CrashLoopBackOff" in output["sections"]["description"]["content"]

    @patch("subprocess.run")
    @patch("boto3.client")
    def test_partial_kubectl_failure_still_produces_report(self, mock_boto_client, mock_subprocess, capsys) -> None:
        """Previous logs failing does not abort — partial report still analyzed."""
        call_index = [0]

        def subprocess_side_effect(*args, **kwargs):
            cmd = args[0]
            idx = call_index[0]
            call_index[0] += 1
            if idx == 0:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=_DESCRIBE_OUTPUT, stderr="")
            elif idx == 1:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=_LOGS_OUTPUT, stderr="")
            elif idx == 2:
                # Previous logs fail (no previous container)
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="previous terminated container not found"
                )
            else:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=_EVENTS_OUTPUT, stderr="")

        mock_subprocess.side_effect = subprocess_side_effect

        analysis = {
            "summary": "OOM kill",
            "hypotheses": ["Memory too low"],
            "fix": "Bump limits",
            "severity": "high",
            "category": "OOM",
        }
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _make_bedrock_response(analysis)
        mock_boto_client.return_value = mock_client

        exit_code = main(["--pod", "test-pod"])
        assert exit_code == 0

        captured = capsys.readouterr()
        # Report still produced with analysis
        assert "AI ANALYSIS" in captured.out
        assert "OOM kill" in captured.out
        # Collection error noted
        assert "Collection error" in captured.out
