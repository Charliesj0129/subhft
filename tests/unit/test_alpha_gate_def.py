"""Unit tests for Gate D, E, F evaluation functions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from hft_platform.alpha._gate_d import _evaluate_gate_d
from hft_platform.alpha._gate_e import _evaluate_gate_e
from hft_platform.alpha._gate_f import _evaluate_gate_f
from hft_platform.alpha._promotion_types import PromotionConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_config(**overrides: Any) -> PromotionConfig:
    defaults: dict[str, Any] = {
        "alpha_id": "test_alpha",
        "owner": "tester",
        "min_sharpe_oos": 1.0,
        "max_abs_drawdown": 0.2,
        "max_turnover": 2.0,
        "max_correlation": 0.7,
    }
    defaults.update(overrides)
    return PromotionConfig(**defaults)


def _passing_scorecard() -> dict[str, Any]:
    return {
        "sharpe_oos": 1.5,
        "max_drawdown": -0.10,
        "turnover": 1.0,
        "correlation_pool_max": 0.3,
        "latency_profile": {
            "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
            "place_order_p95_ms": 35.0,
            "submit_ack_latency_ms": 36.0,
            "modify_ack_latency_ms": 43.0,
            "cancel_ack_latency_ms": 47.0,
        },
    }


# ===================================================================
# Gate D tests
# ===================================================================


class TestGateD:
    """Tests for _evaluate_gate_d."""

    def test_all_pass(self) -> None:
        config = _base_config()
        passed, checks = _evaluate_gate_d(_passing_scorecard(), config)
        assert passed is True
        assert checks["sharpe_oos"]["pass"] is True
        assert checks["max_drawdown"]["pass"] is True
        assert checks["turnover"]["pass"] is True
        assert checks["correlation_pool_max"]["pass"] is True
        assert checks["latency_profile"]["pass"] is True

    def test_sharpe_fail(self) -> None:
        scorecard = _passing_scorecard()
        scorecard["sharpe_oos"] = 0.5
        passed, checks = _evaluate_gate_d(scorecard, _base_config())
        assert passed is False
        assert checks["sharpe_oos"]["pass"] is False

    def test_drawdown_fail(self) -> None:
        scorecard = _passing_scorecard()
        scorecard["max_drawdown"] = -0.30
        passed, checks = _evaluate_gate_d(scorecard, _base_config())
        assert passed is False
        assert checks["max_drawdown"]["pass"] is False

    def test_turnover_fail(self) -> None:
        scorecard = _passing_scorecard()
        scorecard["turnover"] = 3.0
        passed, checks = _evaluate_gate_d(scorecard, _base_config())
        assert passed is False
        assert checks["turnover"]["pass"] is False

    def test_missing_correlation_blocks(self) -> None:
        scorecard = _passing_scorecard()
        del scorecard["correlation_pool_max"]
        passed, checks = _evaluate_gate_d(scorecard, _base_config())
        assert passed is False
        assert checks["correlation_pool_max"]["pass"] is False
        assert "MISSING" in checks["correlation_pool_max"]["detail"]

    def test_missing_latency_profile_blocks(self) -> None:
        scorecard = _passing_scorecard()
        scorecard["latency_profile"] = None
        passed, checks = _evaluate_gate_d(scorecard, _base_config())
        assert passed is False
        assert checks["latency_profile"]["pass"] is False
        assert "MISSING" in checks["latency_profile"]["detail"]

    @patch("hft_platform.alpha._gate_d.FEATURE_SET_VERSION", new="v2", create=True)
    def test_feature_set_version_mismatch_blocks(self) -> None:
        """When both manifest and live FSV are present but differ, Gate D fails."""
        config = _base_config(manifest_feature_set_version="v1")
        scorecard = _passing_scorecard()

        with patch.dict(
            "sys.modules",
            {"hft_platform.feature.registry": MagicMock(FEATURE_SET_VERSION="v2")},
        ):
            passed, checks = _evaluate_gate_d(scorecard, config)

        assert passed is False
        assert checks["feature_set_version"]["pass"] is False
        assert "MISMATCH" in checks["feature_set_version"]["detail"]

    def test_feature_set_version_match_passes(self) -> None:
        config = _base_config(manifest_feature_set_version="v1")
        scorecard = _passing_scorecard()

        with patch.dict(
            "sys.modules",
            {"hft_platform.feature.registry": MagicMock(FEATURE_SET_VERSION="v1")},
        ):
            passed, checks = _evaluate_gate_d(scorecard, config)

        assert passed is True
        assert checks["feature_set_version"]["pass"] is True

    def test_empty_latency_profile_string_blocks(self) -> None:
        """Empty string for latency_profile should also block."""
        scorecard = _passing_scorecard()
        scorecard["latency_profile"] = ""
        passed, checks = _evaluate_gate_d(scorecard, _base_config())
        assert passed is False
        assert checks["latency_profile"]["pass"] is False


# ===================================================================
# Gate E tests
# ===================================================================


class TestGateE:
    """Tests for _evaluate_gate_e."""

    def test_manual_shadow_mode(self, tmp_path: Path) -> None:
        config = _base_config(
            shadow_sessions=10,
            require_paper_trade_governance=False,
        )
        passed, result = _evaluate_gate_e(config, tmp_path)
        assert result["mode"] == "manual_shadow"
        assert "paper_trade_log_available" not in result["checks"]

    def test_paper_trade_governed_mode(self, tmp_path: Path) -> None:
        summary = {
            "session_count": 10,
            "calendar_span_days": 14,
            "distinct_trading_days": 10,
            "min_session_duration_seconds": 7200,
            "invalid_session_duration_count": 0,
            "drift_alerts_total": 0,
            "execution_reject_rate_mean": 0.001,
            "execution_reject_rate_p95": 0.005,
            "regimes_covered": ["trending", "mean_reverting"],
        }
        summary_path = tmp_path / "paper_summary.json"
        summary_path.write_text(json.dumps(summary))

        config = _base_config(
            require_paper_trade_governance=True,
            paper_trade_summary_path=str(summary_path),
        )
        passed, result = _evaluate_gate_e(config, tmp_path)
        assert result["mode"] == "paper_trade_governed"
        assert passed is True
        assert result["checks"]["paper_trade_log_available"]["pass"] is True

    def test_p95_reject_rate_preferred_over_mean(self, tmp_path: Path) -> None:
        """P95 reject rate should be used when available."""
        summary = {
            "session_count": 10,
            "calendar_span_days": 14,
            "distinct_trading_days": 10,
            "min_session_duration_seconds": 7200,
            "invalid_session_duration_count": 0,
            "drift_alerts_total": 0,
            "execution_reject_rate_mean": 0.001,
            "execution_reject_rate_p95": 0.05,  # above threshold
        }
        summary_path = tmp_path / "paper_summary.json"
        summary_path.write_text(json.dumps(summary))

        config = _base_config(
            paper_trade_summary_path=str(summary_path),
            max_execution_reject_rate=0.01,
        )
        passed, result = _evaluate_gate_e(config, tmp_path)
        assert passed is False
        reject_check = result["checks"]["execution_reject_rate"]
        assert reject_check["source"] == "p95"
        assert reject_check["pass"] is False

    def test_mean_reject_rate_fallback(self, tmp_path: Path) -> None:
        """When P95 is absent, falls back to mean."""
        summary = {
            "session_count": 10,
            "drift_alerts_total": 0,
            "execution_reject_rate_mean": 0.005,
            "regimes_covered": ["trending", "mean_reverting"],
        }
        summary_path = tmp_path / "paper_summary.json"
        summary_path.write_text(json.dumps(summary))

        config = _base_config(paper_trade_summary_path=str(summary_path))
        passed, result = _evaluate_gate_e(config, tmp_path)
        reject_check = result["checks"]["execution_reject_rate"]
        assert reject_check["source"] == "mean"

    def test_session_count_below_minimum(self, tmp_path: Path) -> None:
        summary = {
            "session_count": 2,
            "drift_alerts_total": 0,
            "execution_reject_rate_mean": 0.0,
            "regimes_covered": ["trending", "mean_reverting"],
        }
        summary_path = tmp_path / "paper_summary.json"
        summary_path.write_text(json.dumps(summary))

        config = _base_config(
            paper_trade_summary_path=str(summary_path),
            min_shadow_sessions=5,
        )
        passed, result = _evaluate_gate_e(config, tmp_path)
        assert passed is False
        assert result["checks"]["shadow_sessions"]["pass"] is False

    def test_session_duration_check(self, tmp_path: Path) -> None:
        summary = {
            "session_count": 10,
            "calendar_span_days": 14,
            "distinct_trading_days": 10,
            "min_session_duration_seconds": 300,  # only 5 min
            "invalid_session_duration_count": 3,
            "drift_alerts_total": 0,
            "execution_reject_rate_mean": 0.0,
            "regimes_covered": ["trending", "mean_reverting"],
        }
        summary_path = tmp_path / "paper_summary.json"
        summary_path.write_text(json.dumps(summary))

        config = _base_config(
            require_paper_trade_governance=True,
            paper_trade_summary_path=str(summary_path),
            min_paper_trade_session_minutes=60,
        )
        passed, result = _evaluate_gate_e(config, tmp_path)
        assert passed is False
        dur = result["checks"]["paper_trade_session_duration"]
        assert dur["pass"] is False

    def test_drift_alerts_fail(self, tmp_path: Path) -> None:
        summary = {
            "session_count": 10,
            "drift_alerts_total": 3,
            "execution_reject_rate_mean": 0.0,
            "regimes_covered": ["trending", "mean_reverting"],
        }
        summary_path = tmp_path / "paper_summary.json"
        summary_path.write_text(json.dumps(summary))

        config = _base_config(paper_trade_summary_path=str(summary_path))
        passed, result = _evaluate_gate_e(config, tmp_path)
        assert passed is False
        assert result["checks"]["drift_alerts"]["pass"] is False

    def test_regime_span_warning(self, tmp_path: Path) -> None:
        """Less than 2 regimes should produce a warning but not block."""
        summary = {
            "session_count": 10,
            "drift_alerts_total": 0,
            "execution_reject_rate_mean": 0.0,
            "regimes_covered": ["trending"],
        }
        summary_path = tmp_path / "paper_summary.json"
        summary_path.write_text(json.dumps(summary))

        config = _base_config(paper_trade_summary_path=str(summary_path))
        passed, result = _evaluate_gate_e(config, tmp_path)
        regime = result["checks"]["regime_span"]
        assert regime["pass"] is True  # warn-only, does not block
        assert "warning" in regime

    def test_missing_paper_trade_summary_path(self, tmp_path: Path) -> None:
        config = _base_config(
            require_paper_trade_governance=True,
            paper_trade_summary_path=str(tmp_path / "nonexistent.json"),
        )
        passed, result = _evaluate_gate_e(config, tmp_path)
        assert passed is False
        assert result["checks"]["paper_trade_log_available"]["pass"] is False


# ===================================================================
# Gate F tests
# ===================================================================


class TestGateF:
    """Tests for _evaluate_gate_f."""

    def test_gate_disabled(self, tmp_path: Path) -> None:
        config = _base_config(enable_rust_readiness_gate=False)
        passed, result = _evaluate_gate_f(config, tmp_path)
        assert passed is True
        assert result["skipped"] is True

    def test_no_rust_module(self, tmp_path: Path) -> None:
        config = _base_config(
            enable_rust_readiness_gate=True,
            rust_module_name=None,
        )
        with patch(
            "hft_platform.alpha._gate_f._load_rust_module_name",
            return_value="",
        ):
            passed, result = _evaluate_gate_f(config, tmp_path)
        assert passed is False
        assert result["checks"]["rust_module_declared"]["pass"] is False

    def test_parity_test_pass(self, tmp_path: Path) -> None:
        config = _base_config(
            enable_rust_readiness_gate=True,
            rust_module_name="AlphaOFI",
            rust_parity_test_path="tests/unit/test_rust_parity.py",
        )
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="1 passed", stderr="")
        with patch("hft_platform.alpha._gate_f.subprocess.run", return_value=mock_result):
            passed, result = _evaluate_gate_f(config, tmp_path)
        assert passed is True
        assert result["checks"]["rust_parity_tests"]["pass"] is True

    def test_parity_test_fail(self, tmp_path: Path) -> None:
        config = _base_config(
            enable_rust_readiness_gate=True,
            rust_module_name="AlphaOFI",
            rust_parity_test_path="tests/unit/test_rust_parity.py",
        )
        mock_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="FAILED", stderr="assertion error")
        with patch("hft_platform.alpha._gate_f.subprocess.run", return_value=mock_result):
            passed, result = _evaluate_gate_f(config, tmp_path)
        assert passed is False
        assert result["checks"]["rust_parity_tests"]["pass"] is False
        assert result["checks"]["rust_parity_tests"]["returncode"] == 1

    def test_parity_test_timeout(self, tmp_path: Path) -> None:
        config = _base_config(
            enable_rust_readiness_gate=True,
            rust_module_name="AlphaOFI",
            rust_parity_test_path="tests/unit/test_rust_parity.py",
            rust_parity_timeout_s=30,
        )
        with patch(
            "hft_platform.alpha._gate_f.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=30, output="", stderr=""),
        ):
            passed, result = _evaluate_gate_f(config, tmp_path)
        assert passed is False
        assert result["checks"]["rust_parity_tests"]["returncode"] == 124

    def test_benchmark_gate_pass(self, tmp_path: Path) -> None:
        config = _base_config(
            enable_rust_readiness_gate=True,
            rust_module_name="AlphaOFI",
            enforce_rust_benchmark_gate=True,
        )
        parity_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        bench_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        with patch(
            "hft_platform.alpha._gate_f.subprocess.run",
            side_effect=[parity_result, bench_result],
        ):
            passed, result = _evaluate_gate_f(config, tmp_path)
        assert passed is True
        assert result["checks"]["rust_perf_regression_gate"]["pass"] is True

    def test_benchmark_gate_fail(self, tmp_path: Path) -> None:
        config = _base_config(
            enable_rust_readiness_gate=True,
            rust_module_name="AlphaOFI",
            enforce_rust_benchmark_gate=True,
        )
        parity_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        bench_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="REGRESSION", stderr="")
        with patch(
            "hft_platform.alpha._gate_f.subprocess.run",
            side_effect=[parity_result, bench_result],
        ):
            passed, result = _evaluate_gate_f(config, tmp_path)
        assert passed is False
        assert result["checks"]["rust_perf_regression_gate"]["pass"] is False

    def test_no_rust_module_with_benchmark_gate(self, tmp_path: Path) -> None:
        """When no rust module and benchmark gate is on, both checks fail."""
        config = _base_config(
            enable_rust_readiness_gate=True,
            rust_module_name=None,
            enforce_rust_benchmark_gate=True,
        )
        with patch(
            "hft_platform.alpha._gate_f._load_rust_module_name",
            return_value="",
        ):
            passed, result = _evaluate_gate_f(config, tmp_path)
        assert passed is False
        assert result["checks"]["rust_module_declared"]["pass"] is False
        assert result["checks"]["rust_parity_tests"]["pass"] is False
        assert result["checks"]["rust_perf_regression_gate"]["pass"] is False
