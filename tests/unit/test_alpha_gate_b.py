"""Unit tests for hft_platform.alpha._gate_b — Gate B pytest execution gate.

Tests cover: skip mode, success/failure propagation, timeout handling,
alpha_id validation, and output truncation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.alpha._gate_b import run_gate_b
from hft_platform.alpha._validation_types import GateReport


class TestRunGateBSkip:
    def test_skip_returns_passed(self, tmp_path: Path):
        report = run_gate_b("ofi_mc", tmp_path, skip_tests=True)
        assert isinstance(report, GateReport)
        assert report.passed is True
        assert report.details["skipped"] is True

    def test_skip_includes_reason(self, tmp_path: Path):
        report = run_gate_b("ofi_mc", tmp_path, skip_tests=True)
        assert "skip_gate_b" in report.details["reason"]


class TestRunGateBAlphaIdValidation:
    def test_rejects_invalid_alpha_id(self, tmp_path: Path):
        with pytest.raises(ValueError, match="Invalid alpha_id"):
            run_gate_b("INVALID-ID", tmp_path)

    def test_rejects_empty_alpha_id(self, tmp_path: Path):
        with pytest.raises(ValueError, match="Invalid alpha_id"):
            run_gate_b("", tmp_path)

    def test_rejects_special_chars(self, tmp_path: Path):
        with pytest.raises(ValueError, match="Invalid alpha_id"):
            run_gate_b("../escape", tmp_path)

    def test_rejects_uppercase_start(self, tmp_path: Path):
        with pytest.raises(ValueError, match="Invalid alpha_id"):
            run_gate_b("Abc", tmp_path)

    def test_accepts_valid_alpha_id(self, tmp_path: Path):
        # Should not raise — will proceed to subprocess call
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            report = run_gate_b("valid_alpha_id", tmp_path)
            assert report.gate == "Gate B"


class TestRunGateBSuccess:
    def test_passed_when_returncode_zero(self, tmp_path: Path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="all passed", stderr="")
            report = run_gate_b("ofi_mc", tmp_path)
            assert report.passed is True
            assert report.details["returncode"] == 0

    def test_command_includes_test_path(self, tmp_path: Path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run_gate_b("ofi_mc", tmp_path)
            call_args = mock_run.call_args
            cmd = call_args[0][0]
            expected_test_path = str(tmp_path / "research" / "alphas" / "ofi_mc" / "tests")
            assert expected_test_path in cmd

    def test_cwd_set_to_project_root(self, tmp_path: Path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run_gate_b("ofi_mc", tmp_path)
            call_args = mock_run.call_args
            assert call_args[1]["cwd"] == str(tmp_path)


class TestRunGateBFailure:
    def test_failed_when_returncode_nonzero(self, tmp_path: Path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="FAILED", stderr="traceback")
            report = run_gate_b("ofi_mc", tmp_path)
            assert report.passed is False
            assert report.details["returncode"] == 1
            assert "FAILED" in report.details["stdout_tail"]
            assert "traceback" in report.details["stderr_tail"]

    def test_returncode_2(self, tmp_path: Path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=2, stdout="error", stderr="fatal")
            report = run_gate_b("ofi_mc", tmp_path)
            assert report.passed is False
            assert report.details["returncode"] == 2


class TestRunGateBTimeout:
    def test_timeout_returns_failed(self, tmp_path: Path):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["pytest"], timeout=10, output="partial output")
            report = run_gate_b("ofi_mc", tmp_path, timeout_s=10)
            assert report.passed is False
            assert "timeout" in report.details["error"]
            assert "10" in report.details["error"]

    def test_timeout_with_none_stdout(self, tmp_path: Path):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["pytest"], timeout=5, output=None)
            report = run_gate_b("ofi_mc", tmp_path, timeout_s=5)
            assert report.passed is False
            assert report.details["stdout_tail"] == ""

    def test_custom_timeout_value(self, tmp_path: Path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run_gate_b("ofi_mc", tmp_path, timeout_s=60)
            call_args = mock_run.call_args
            assert call_args[1]["timeout"] == 60


class TestRunGateBOutputTruncation:
    def test_stdout_truncated_to_4000(self, tmp_path: Path):
        long_output = "x" * 10000
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=long_output, stderr="")
            report = run_gate_b("ofi_mc", tmp_path)
            assert len(report.details["stdout_tail"]) == 4000

    def test_stderr_truncated_to_2000(self, tmp_path: Path):
        long_stderr = "e" * 5000
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr=long_stderr)
            report = run_gate_b("ofi_mc", tmp_path)
            assert len(report.details["stderr_tail"]) == 2000

    def test_short_output_not_truncated(self, tmp_path: Path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="short", stderr="ok")
            report = run_gate_b("ofi_mc", tmp_path)
            assert report.details["stdout_tail"] == "short"
            assert report.details["stderr_tail"] == "ok"


class TestRunGateBCommandFormat:
    def test_command_uses_uv_run_pytest(self, tmp_path: Path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            report = run_gate_b("ofi_mc", tmp_path)
            cmd_str = report.details["command"]
            assert "uv run python -m pytest" in cmd_str
            assert "--no-cov" in cmd_str
            assert "-q" in cmd_str
