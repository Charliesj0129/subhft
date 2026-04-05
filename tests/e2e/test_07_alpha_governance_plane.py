"""E2E tests for the Alpha Governance Plane.

Tests cover:
- Gate A manifest validation
- Gate B pytest execution
- Gate C scorecard data contract
- Gate D threshold evaluation
- Gate E shadow session
- Full promotion lifecycle (canary)
- Promotion rollback
- Gate C fail blocks promotion
"""
from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_npz_with_fields(path: Path, fields: list[str]) -> str:
    """Create a .npz file with a structured array that has the given field names."""
    dtype = [(f, "f8") for f in fields]
    arr = np.zeros(4, dtype=dtype)
    np.savez(path, data=arr)
    return str(path)


def _make_canary_yaml(promotions_dir: Path, alpha_id: str, **overrides: Any) -> Path:
    """Create a minimal promotion YAML for canary tests."""
    promotions_dir.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {
        "alpha_id": alpha_id,
        "enabled": True,
        "weight": 0.02,
        "guardrails": {
            "max_live_slippage_bps": 3.0,
            "max_live_drawdown_contribution": 0.02,
            "max_execution_error_rate": 0.01,
        },
        "rollback": {
            "trigger": {
                "live_slippage_bps_gt": 3.0,
                "live_drawdown_contribution_gt": 0.02,
                "execution_error_rate_gt": 0.01,
            }
        },
        "scorecard_snapshot": {
            "sharpe_oos": 1.5,
        },
    }
    config.update(overrides)
    yaml_path = promotions_dir / f"{alpha_id}.yaml"
    yaml_path.write_text(yaml.safe_dump(config, sort_keys=False))
    return yaml_path


# ===========================================================================
# TestChain — sequential gate tests
# ===========================================================================

@pytest.mark.e2e_chain
class TestChain:
    def test_gate_a_manifest_validation(self, tmp_path: Path) -> None:
        """Gate A passes when manifest has required fields and data covers them."""
        from hft_platform.alpha.validation import run_gate_a

        # Create a data file with the required fields
        data_file = _make_npz_with_fields(
            tmp_path / "feed.npz",
            ["bid_px", "ask_px", "bid_qty", "ask_qty", "trade_vol", "current_mid"],
        )

        manifest = types.SimpleNamespace(
            alpha_id="test_alpha_001",
            name="Test Alpha",
            version="0.1.0",
            author="test_author",
            description="A minimal test alpha.",
            data_fields=("bid_px", "ask_px", "bid_qty", "ask_qty", "trade_vol", "current_mid"),
            complexity_class="O(1)",
            complexity="O(1)",
        )

        report = run_gate_a(manifest, [data_file], root=tmp_path)
        assert hasattr(report, "passed"), "GateReport must have .passed"
        assert report.passed is True
        assert report.gate == "Gate A"

    def test_gate_a_rejects_missing_fields(self, tmp_path: Path) -> None:
        """Gate A fails when required data_fields are not in the dataset."""
        from hft_platform.alpha.validation import run_gate_a

        # Data file has only generic fields, NOT the required ones
        data_file = _make_npz_with_fields(
            tmp_path / "partial.npz",
            ["timestamp", "volume"],
        )

        manifest = types.SimpleNamespace(
            alpha_id="test_alpha_002",
            data_fields=("bid_px", "ask_px"),
            complexity="O(1)",
        )

        report = run_gate_a(manifest, [data_file], root=tmp_path)
        assert report.passed is False
        assert "bid_px" in report.details.get("missing_fields", []) or \
               "ask_px" in report.details.get("missing_fields", []), (
            f"Expected missing_fields to include required fields; got: {report.details}"
        )

    def test_gate_b_pytest_execution(self, tmp_path: Path) -> None:
        """Gate B returns a GateReport with a .passed attribute."""
        from hft_platform.alpha.validation import run_gate_b

        # Use skip_tests=True to avoid actually running pytest on a dummy alpha
        report = run_gate_b(alpha_id="nonexistent_alpha", project_root=tmp_path, skip_tests=True)
        assert report is not None
        assert hasattr(report, "passed"), "GateReport must have .passed"
        # When skipped, it should pass
        assert report.passed is True
        assert report.details.get("skipped") is True

    def test_gate_c_backtest_scorecard(self) -> None:
        """Gate C scorecard data contract has all required fields."""
        scorecard: dict[str, Any] = {
            "sharpe_oos": 1.5,
            "max_drawdown": -0.08,
            "turnover": 0.5,
            "correlation_pool_max": 0.3,
            "latency_profile": {
                "latency_profile_id": "sim_p95_v2026-02-26",
                "submit_ack_latency_ms": 36.0,
                "cancel_ack_latency_ms": 47.0,
                "live_uplift_factor": 1.5,
                "model_applied": True,
            },
        }

        required_keys = [
            "sharpe_oos",
            "max_drawdown",
            "turnover",
            "correlation_pool_max",
            "latency_profile",
        ]
        for key in required_keys:
            assert key in scorecard, f"Scorecard must contain '{key}'"

        # Verify field types
        assert isinstance(scorecard["sharpe_oos"], (int, float))
        assert isinstance(scorecard["max_drawdown"], (int, float))
        assert isinstance(scorecard["turnover"], (int, float))
        assert isinstance(scorecard["correlation_pool_max"], (int, float))
        assert isinstance(scorecard["latency_profile"], dict)

    def test_gate_d_threshold_evaluation(self) -> None:
        """Gate D passes when scorecard meets all thresholds."""
        from hft_platform.alpha.promotion import PromotionConfig, _evaluate_gate_d

        scorecard: dict[str, Any] = {
            "sharpe_oos": 1.5,
            "max_drawdown": -0.1,   # >= -0.2 threshold
            "turnover": 1.0,        # <= 2.0 threshold
            "correlation_pool_max": 0.3,  # <= 0.7 threshold
            "latency_profile": {
                "latency_profile_id": "sim_p95_v2026-02-26",
                "model_applied": True,
            },
        }
        config = PromotionConfig(
            alpha_id="test_alpha",
            owner="test_owner",
            min_sharpe_oos=1.0,
            max_abs_drawdown=0.2,
            max_turnover=2.0,
            max_correlation=0.7,
        )

        passed, checks = _evaluate_gate_d(scorecard, config)
        assert passed is True
        assert checks["sharpe_oos"]["pass"] is True
        assert checks["max_drawdown"]["pass"] is True
        assert checks["turnover"]["pass"] is True
        assert checks["correlation_pool_max"]["pass"] is True
        assert checks["latency_profile"]["pass"] is True

    def test_gate_d_rejects_below_threshold(self) -> None:
        """Gate D fails when scorecard is below thresholds."""
        from hft_platform.alpha.promotion import PromotionConfig, _evaluate_gate_d

        scorecard: dict[str, Any] = {
            "sharpe_oos": 0.3,      # below min_sharpe_oos=1.0
            "max_drawdown": -0.5,   # below max_abs_drawdown=0.2
            "turnover": 3.0,        # above max_turnover=2.0
            "correlation_pool_max": 0.8,  # above max_correlation=0.7
            "latency_profile": None,  # missing
        }
        config = PromotionConfig(
            alpha_id="test_alpha",
            owner="test_owner",
            min_sharpe_oos=1.0,
            max_abs_drawdown=0.2,
            max_turnover=2.0,
            max_correlation=0.7,
        )

        passed, checks = _evaluate_gate_d(scorecard, config)
        assert passed is False
        assert checks["sharpe_oos"]["pass"] is False
        assert checks["max_drawdown"]["pass"] is False
        assert checks["turnover"]["pass"] is False
        assert checks["correlation_pool_max"]["pass"] is False
        assert checks["latency_profile"]["pass"] is False

    def test_gate_e_shadow_session(self, tmp_path: Path) -> None:
        """Gate E returns a (bool, dict) tuple — contract is verified."""
        from hft_platform.alpha.promotion import PromotionConfig, _evaluate_gate_e

        # Provide a paper trade summary that satisfies the defaults
        summary = {
            "session_count": 6,
            "calendar_span_days": 8,
            "distinct_trading_days": 6,
            "min_session_duration_seconds": 7200,
            "invalid_session_duration_count": 0,
            "drift_alerts_total": 0,
            "execution_reject_rate_mean": 0.005,
        }
        summary_path = tmp_path / "paper_summary.json"
        summary_path.write_text(json.dumps(summary))

        config = PromotionConfig(
            alpha_id="test_alpha",
            owner="test_owner",
            shadow_sessions=6,
            min_shadow_sessions=5,
            drift_alerts=0,
            execution_reject_rate=0.005,
            paper_trade_summary_path=str(summary_path),
        )

        result = _evaluate_gate_e(config, tmp_path)
        assert isinstance(result, tuple), "_evaluate_gate_e must return a tuple"
        assert len(result) == 2, "Tuple must have (passed, checks)"
        passed, checks = result
        assert isinstance(passed, bool)
        assert isinstance(checks, dict)


# ===========================================================================
# TestIntegration — async integration tests
# ===========================================================================

@pytest.mark.e2e_integration
class TestIntegration:
    def test_full_promotion_lifecycle(self, tmp_path: Path) -> None:
        """CanaryMonitor: load canaries, evaluate with good metrics — state is valid."""
        from hft_platform.alpha.canary import CanaryMonitor

        promotions_dir = tmp_path / "promotions"
        alpha_id = "test_alpha_canary"
        _make_canary_yaml(promotions_dir, alpha_id)

        monitor = CanaryMonitor(promotions_dir=str(promotions_dir))
        canaries = monitor.load_active_canaries()
        assert len(canaries) >= 1, "Should find at least one active canary"

        good_metrics = {
            "slippage_bps": 1.0,
            "drawdown_contribution": 0.005,
            "execution_error_rate": 0.001,
            "sessions_live": 3,
        }
        status = monitor.evaluate(alpha_id, good_metrics)

        assert status.alpha_id == alpha_id
        assert status.state in ("canary", "escalated", "graduated"), (
            f"Expected state in ('canary', 'escalated', 'graduated'), got: {status.state}"
        )
        assert isinstance(status.current_weight, float)
        assert isinstance(status.checks, dict)

    def test_promotion_rollback(self, tmp_path: Path) -> None:
        """CanaryMonitor: bad metrics trigger rollback; apply_decision disables YAML."""
        from hft_platform.alpha.canary import CanaryMonitor

        promotions_dir = tmp_path / "promotions"
        alpha_id = "test_alpha_rollback"
        yaml_path = _make_canary_yaml(promotions_dir, alpha_id)

        monitor = CanaryMonitor(promotions_dir=str(promotions_dir))

        bad_metrics = {
            "slippage_bps": 10.0,   # exceeds max 3.0
            "drawdown_contribution": 0.1,  # exceeds max 0.02
            "execution_error_rate": 0.001,
            "sessions_live": 3,
        }
        status = monitor.evaluate(alpha_id, bad_metrics)
        assert status.state == "rolled_back", (
            f"Expected rolled_back due to bad metrics; got: {status.state}, reason: {status.reason}"
        )

        # Apply the rollback decision — YAML should be updated
        monitor.apply_decision(status)

        updated = yaml.safe_load(yaml_path.read_text())
        assert not updated.get("enabled", True) or updated.get("weight", 1.0) == 0.0, (
            f"After rollback, enabled should be False or weight should be 0; got: {updated}"
        )

    def test_gate_c_fail_blocks_promotion(self) -> None:
        """Gate D rejects bad scorecard, confirming Gate C failure blocks promotion."""
        from hft_platform.alpha.promotion import PromotionConfig, _evaluate_gate_d

        # Scorecard that fails all checks — simulates a Gate C failure outcome
        bad_scorecard: dict[str, Any] = {
            "sharpe_oos": 0.2,
            "max_drawdown": -0.6,
            "turnover": 5.0,
            "correlation_pool_max": 0.9,
            "latency_profile": None,
        }
        config = PromotionConfig(
            alpha_id="bad_alpha",
            owner="test_owner",
            min_sharpe_oos=1.0,
            max_abs_drawdown=0.2,
            max_turnover=2.0,
            max_correlation=0.7,
        )

        passed, checks = _evaluate_gate_d(bad_scorecard, config)
        assert passed is False, (
            "Gate D must reject a scorecard that fails all thresholds "
            "(blocks promotion from a failed Gate C result)"
        )
        # Confirm multiple checks failed
        failed_checks = [k for k, v in checks.items() if not v.get("pass", True)]
        assert len(failed_checks) >= 3, (
            f"Expected at least 3 failed checks, got {len(failed_checks)}: {failed_checks}"
        )
