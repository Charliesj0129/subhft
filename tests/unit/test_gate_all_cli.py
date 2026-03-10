"""Tests for the run-gate-all CLI subcommand in research/factory.py."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Patch targets: since cmd_run_gate_all imports these inside the function body
# from `src.hft_platform.alpha.validation` and `src.hft_platform.alpha.promotion`,
# we patch at the source module level.
_V = "src.hft_platform.alpha.validation"
_P = "src.hft_platform.alpha.promotion"
_F = "research.factory"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gate_report(gate: str, passed: bool, details: dict[str, Any] | None = None) -> MagicMock:
    report = MagicMock()
    report.gate = gate
    report.passed = passed
    report.details = details or {}
    return report


def _make_manifest(alpha_id: str = "test_alpha") -> MagicMock:
    manifest = MagicMock()
    manifest.alpha_id = alpha_id
    manifest.data_fields = ("price", "volume")
    manifest.paper_refs = ()
    manifest.feature_set_version = "lob_shared_v1"
    return manifest


def _make_alpha_instance(alpha_id: str = "test_alpha") -> MagicMock:
    alpha = MagicMock()
    alpha.manifest = _make_manifest(alpha_id)
    return alpha


def _latency_profile() -> dict[str, Any]:
    return {
        "submit_ack_latency_ms": 36.0,
        "modify_ack_latency_ms": 43.0,
        "cancel_ack_latency_ms": 47.0,
        "local_decision_pipeline_latency_us": 250,
    }


def _build_args(**overrides: Any) -> argparse.Namespace:
    defaults = {
        "alpha_id": "test_alpha",
        "data": ["fake.npy"],
        "oos_split": 0.7,
        "latency_profile": "test_profile",
        "skip_gate_b": False,
        "skip_gate_e": True,
        "shadow_sessions": 5,
        "opt_threshold_min": 0.01,
        "no_opt": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _setup_registry(mock_cls: MagicMock, alpha_id: str = "test_alpha") -> MagicMock:
    alpha = _make_alpha_instance(alpha_id)
    registry = MagicMock()
    registry.discover.return_value = {alpha_id: alpha}
    mock_cls.return_value = registry
    return alpha


def _write_scorecard(path: str, **extra: Any) -> str:
    data = {
        "sharpe_oos": 2.0,
        "max_drawdown": -0.05,
        "turnover": 0.5,
        "correlation_pool_max": 0.3,
        "latency_profile": "test",
    }
    data.update(extra)
    Path(path).write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------

class TestGateAllArgParsing:
    def test_required_args(self) -> None:
        from research.factory import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "run-gate-all", "my_alpha",
            "--data", "data1.npy", "data2.npy",
        ])
        assert args.command == "run-gate-all"
        assert args.alpha_id == "my_alpha"
        assert args.data == ["data1.npy", "data2.npy"]
        assert args.oos_split == 0.7
        assert args.skip_gate_b is False
        assert args.skip_gate_e is False
        assert args.shadow_sessions == 5

    def test_optional_args(self) -> None:
        from research.factory import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "run-gate-all", "my_alpha",
            "--data", "data.npy",
            "--oos-split", "0.8",
            "--latency-profile", "custom_profile",
            "--skip-gate-b",
            "--skip-gate-e",
            "--shadow-sessions", "10",
            "--no-opt",
            "--opt-threshold-min", "0.05",
        ])
        assert args.oos_split == 0.8
        assert args.latency_profile == "custom_profile"
        assert args.skip_gate_b is True
        assert args.skip_gate_e is True
        assert args.shadow_sessions == 10
        assert args.no_opt is True
        assert args.opt_threshold_min == 0.05

    def test_missing_data_arg_fails(self) -> None:
        from research.factory import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["run-gate-all", "my_alpha"])


# ---------------------------------------------------------------------------
# Sequential gate execution tests
# ---------------------------------------------------------------------------

class TestGateAllExecution:

    @patch(f"{_P}._evaluate_gate_d")
    @patch(f"{_V}.run_gate_c")
    @patch(f"{_V}.run_gate_b")
    @patch(f"{_V}.run_gate_a")
    @patch("research.registry.alpha_registry.AlphaRegistry")
    @patch("research.tools.latency_profiles.load_latency_profile", return_value=_latency_profile())
    @patch(f"{_F}._save_gate_all_report")
    def test_all_gates_pass(
        self,
        mock_save: MagicMock,
        mock_load_latency: MagicMock,
        mock_registry_cls: MagicMock,
        mock_gate_a: MagicMock,
        mock_gate_b: MagicMock,
        mock_gate_c: MagicMock,
        mock_gate_d: MagicMock,
    ) -> None:
        from research.factory import cmd_run_gate_all

        _setup_registry(mock_registry_cls)

        mock_gate_a.return_value = _make_gate_report("Gate A", True)
        mock_gate_b.return_value = _make_gate_report("Gate B", True, {"tests_passed": 24})

        sc = _write_scorecard("/tmp/test_sc_all.json")
        gate_c_report = _make_gate_report("Gate C", True, {"sharpe_oos": 2.0, "ic_mean": 0.3, "max_drawdown": -0.05})
        mock_gate_c.return_value = (gate_c_report, "run_123", "hash_abc", sc, "/tmp/meta.json")

        mock_gate_d.return_value = (True, {"sharpe_oos": {"pass": True}})

        result = cmd_run_gate_all(_build_args(skip_gate_e=True))
        assert result == 0
        mock_gate_a.assert_called_once()
        mock_gate_b.assert_called_once()
        mock_gate_c.assert_called_once()
        mock_gate_d.assert_called_once()

    @patch(f"{_V}.run_gate_a")
    @patch("research.registry.alpha_registry.AlphaRegistry")
    @patch("research.tools.latency_profiles.load_latency_profile", return_value=_latency_profile())
    @patch(f"{_F}._save_gate_all_report")
    def test_stop_on_gate_a_failure(
        self,
        mock_save: MagicMock,
        mock_load_latency: MagicMock,
        mock_registry_cls: MagicMock,
        mock_gate_a: MagicMock,
    ) -> None:
        from research.factory import cmd_run_gate_all

        _setup_registry(mock_registry_cls)
        mock_gate_a.return_value = _make_gate_report("Gate A", False, {"error": "bad manifest"})

        result = cmd_run_gate_all(_build_args())
        assert result == 1

    @patch(f"{_V}.run_gate_b")
    @patch(f"{_V}.run_gate_a")
    @patch("research.registry.alpha_registry.AlphaRegistry")
    @patch("research.tools.latency_profiles.load_latency_profile", return_value=_latency_profile())
    @patch(f"{_F}._save_gate_all_report")
    def test_stop_on_gate_b_failure_skips_gate_c(
        self,
        mock_save: MagicMock,
        mock_load_latency: MagicMock,
        mock_registry_cls: MagicMock,
        mock_gate_a: MagicMock,
        mock_gate_b: MagicMock,
    ) -> None:
        from research.factory import cmd_run_gate_all

        _setup_registry(mock_registry_cls)
        mock_gate_a.return_value = _make_gate_report("Gate A", True)
        mock_gate_b.return_value = _make_gate_report("Gate B", False, {"stderr_tail": "test failed"})

        with patch(f"{_V}.run_gate_c") as mock_gate_c:
            result = cmd_run_gate_all(_build_args())
            assert result == 1
            mock_gate_c.assert_not_called()

    @patch(f"{_V}.run_gate_c")
    @patch(f"{_V}.run_gate_b")
    @patch(f"{_V}.run_gate_a")
    @patch("research.registry.alpha_registry.AlphaRegistry")
    @patch("research.tools.latency_profiles.load_latency_profile", return_value=_latency_profile())
    @patch(f"{_F}._save_gate_all_report")
    def test_stop_on_gate_c_failure(
        self,
        mock_save: MagicMock,
        mock_load_latency: MagicMock,
        mock_registry_cls: MagicMock,
        mock_gate_a: MagicMock,
        mock_gate_b: MagicMock,
        mock_gate_c: MagicMock,
    ) -> None:
        from research.factory import cmd_run_gate_all

        _setup_registry(mock_registry_cls)
        mock_gate_a.return_value = _make_gate_report("Gate A", True)
        mock_gate_b.return_value = _make_gate_report("Gate B", True)

        gate_c_report = _make_gate_report("Gate C", False, {"sharpe_oos": 0.1})
        mock_gate_c.return_value = (gate_c_report, "run_x", "hash_x", None, None)

        result = cmd_run_gate_all(_build_args(skip_gate_e=True))
        assert result == 1

    @patch(f"{_P}._evaluate_gate_d")
    @patch(f"{_V}.run_gate_c")
    @patch(f"{_V}.run_gate_b")
    @patch(f"{_V}.run_gate_a")
    @patch("research.registry.alpha_registry.AlphaRegistry")
    @patch("research.tools.latency_profiles.load_latency_profile", return_value=_latency_profile())
    @patch(f"{_F}._save_gate_all_report")
    def test_stop_on_gate_d_failure(
        self,
        mock_save: MagicMock,
        mock_load_latency: MagicMock,
        mock_registry_cls: MagicMock,
        mock_gate_a: MagicMock,
        mock_gate_b: MagicMock,
        mock_gate_c: MagicMock,
        mock_gate_d: MagicMock,
    ) -> None:
        from research.factory import cmd_run_gate_all

        _setup_registry(mock_registry_cls)
        mock_gate_a.return_value = _make_gate_report("Gate A", True)
        mock_gate_b.return_value = _make_gate_report("Gate B", True)

        sc = _write_scorecard("/tmp/test_sc_d.json", sharpe_oos=0.5)
        gate_c_report = _make_gate_report("Gate C", True, {"sharpe_oos": 0.5, "ic_mean": 0.1, "max_drawdown": -0.1})
        mock_gate_c.return_value = (gate_c_report, "run_d", "hash_d", sc, "/tmp/meta.json")

        mock_gate_d.return_value = (False, {"sharpe_oos": {"pass": False, "value": 0.5, "min": 1.0}})

        result = cmd_run_gate_all(_build_args(skip_gate_e=True))
        assert result == 1

    @patch(f"{_P}._evaluate_gate_e")
    @patch(f"{_P}._evaluate_gate_d")
    @patch(f"{_V}.run_gate_c")
    @patch(f"{_V}.run_gate_b")
    @patch(f"{_V}.run_gate_a")
    @patch("research.registry.alpha_registry.AlphaRegistry")
    @patch("research.tools.latency_profiles.load_latency_profile", return_value=_latency_profile())
    @patch(f"{_F}._save_gate_all_report")
    def test_gate_e_runs_when_not_skipped(
        self,
        mock_save: MagicMock,
        mock_load_latency: MagicMock,
        mock_registry_cls: MagicMock,
        mock_gate_a: MagicMock,
        mock_gate_b: MagicMock,
        mock_gate_c: MagicMock,
        mock_gate_d: MagicMock,
        mock_gate_e: MagicMock,
    ) -> None:
        from research.factory import cmd_run_gate_all

        _setup_registry(mock_registry_cls)
        mock_gate_a.return_value = _make_gate_report("Gate A", True)
        mock_gate_b.return_value = _make_gate_report("Gate B", True)

        sc = _write_scorecard("/tmp/test_sc_e.json")
        gate_c_report = _make_gate_report("Gate C", True, {"sharpe_oos": 2.0, "ic_mean": 0.3, "max_drawdown": -0.05})
        mock_gate_c.return_value = (gate_c_report, "run_e", "hash_e", sc, "/tmp/meta.json")

        mock_gate_d.return_value = (True, {"sharpe_oos": {"pass": True}})
        mock_gate_e.return_value = (True, {"mode": "manual_shadow", "checks": {}})

        result = cmd_run_gate_all(_build_args(skip_gate_e=False))
        assert result == 0
        mock_gate_e.assert_called_once()

    @patch(f"{_P}._evaluate_gate_d")
    @patch(f"{_V}.run_gate_c")
    @patch(f"{_V}.run_gate_b")
    @patch(f"{_V}.run_gate_a")
    @patch("research.registry.alpha_registry.AlphaRegistry")
    @patch("research.tools.latency_profiles.load_latency_profile", return_value=_latency_profile())
    @patch(f"{_F}._save_gate_all_report")
    def test_skip_gate_e_flag(
        self,
        mock_save: MagicMock,
        mock_load_latency: MagicMock,
        mock_registry_cls: MagicMock,
        mock_gate_a: MagicMock,
        mock_gate_b: MagicMock,
        mock_gate_c: MagicMock,
        mock_gate_d: MagicMock,
    ) -> None:
        from research.factory import cmd_run_gate_all

        _setup_registry(mock_registry_cls)
        mock_gate_a.return_value = _make_gate_report("Gate A", True)
        mock_gate_b.return_value = _make_gate_report("Gate B", True)

        sc = _write_scorecard("/tmp/test_sc_skip.json")
        gate_c_report = _make_gate_report("Gate C", True, {"sharpe_oos": 2.0, "ic_mean": 0.3, "max_drawdown": -0.05})
        mock_gate_c.return_value = (gate_c_report, "run_s", "hash_s", sc, "/tmp/meta.json")

        mock_gate_d.return_value = (True, {"sharpe_oos": {"pass": True}})

        result = cmd_run_gate_all(_build_args(skip_gate_e=True))
        assert result == 0


# ---------------------------------------------------------------------------
# Summary output tests
# ---------------------------------------------------------------------------

class TestGateAllSummary:

    def test_print_summary_all_pass(self, capsys: pytest.CaptureFixture[str]) -> None:
        from research.factory import _print_gate_all_summary

        results: dict[str, dict[str, Any]] = {
            "Gate A": {"passed": True, "detail": "OK"},
            "Gate B": {"passed": True, "detail": "24 tests"},
            "Gate C": {"passed": True, "detail": "Sharpe=2.0"},
            "Gate D": {"passed": True, "detail": "thresholds met"},
            "Gate E": {"passed": True, "detail": "sessions OK"},
        }
        _print_gate_all_summary("test_alpha", results)
        out = capsys.readouterr().out
        assert "PASS" in out
        assert "APPROVED" in out

    def test_print_summary_with_failure(self, capsys: pytest.CaptureFixture[str]) -> None:
        from research.factory import _print_gate_all_summary

        results: dict[str, dict[str, Any]] = {
            "Gate A": {"passed": True, "detail": "OK"},
            "Gate B": {"passed": False, "detail": "tests failed"},
        }
        _print_gate_all_summary("test_alpha", results, skip_gate_e=True)
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "REJECTED" in out
        assert "not reached" in out

    def test_print_summary_skip_gate_e(self, capsys: pytest.CaptureFixture[str]) -> None:
        from research.factory import _print_gate_all_summary

        results: dict[str, dict[str, Any]] = {
            "Gate A": {"passed": True, "detail": "OK"},
            "Gate B": {"passed": True, "detail": "OK"},
            "Gate C": {"passed": True, "detail": "OK"},
            "Gate D": {"passed": True, "detail": "OK"},
        }
        _print_gate_all_summary("test_alpha", results, skip_gate_e=True)
        out = capsys.readouterr().out
        assert "SKIP" in out

    def test_save_report_payload(self) -> None:
        from research.factory import _save_gate_all_report

        results: dict[str, dict[str, Any]] = {
            "Gate A": {"passed": True, "detail": "OK"},
            "Gate B": {"passed": True, "detail": "tests passed"},
        }

        with patch(f"{_F}._write_json") as mock_write:
            _save_gate_all_report("test_alpha", results, "test_run_001")
            mock_write.assert_called_once()
            payload = mock_write.call_args[0][1]
            assert payload["alpha_id"] == "test_alpha"
            assert payload["run_id"] == "test_run_001"
            assert payload["overall_passed"] is True
            assert "Gate A" in payload["gates"]
            assert payload["gates"]["Gate A"]["passed"] is True


# ---------------------------------------------------------------------------
# Exit code tests
# ---------------------------------------------------------------------------

class TestGateAllExitCode:

    @patch(f"{_V}.run_gate_a")
    @patch("research.registry.alpha_registry.AlphaRegistry")
    @patch("research.tools.latency_profiles.load_latency_profile", return_value=_latency_profile())
    @patch(f"{_F}._save_gate_all_report")
    def test_exit_1_on_failure(
        self,
        mock_save: MagicMock,
        mock_load_latency: MagicMock,
        mock_registry_cls: MagicMock,
        mock_gate_a: MagicMock,
    ) -> None:
        from research.factory import cmd_run_gate_all

        _setup_registry(mock_registry_cls)
        mock_gate_a.return_value = _make_gate_report("Gate A", False)

        result = cmd_run_gate_all(_build_args())
        assert result == 1

    @patch("research.registry.alpha_registry.AlphaRegistry")
    @patch("research.tools.latency_profiles.load_latency_profile", return_value=_latency_profile())
    def test_exit_1_alpha_not_found(
        self,
        mock_load_latency: MagicMock,
        mock_registry_cls: MagicMock,
    ) -> None:
        from research.factory import cmd_run_gate_all

        registry = MagicMock()
        registry.discover.return_value = {}
        mock_registry_cls.return_value = registry

        result = cmd_run_gate_all(_build_args())
        assert result == 1
