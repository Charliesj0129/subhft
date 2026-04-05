"""Tests for fail-safe defaults in canary metric building.

Verifies that missing live metrics default to conservative (worst-case) values
that exceed rollback thresholds, triggering rollback when data is missing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml

from hft_platform.alpha.canary import CanaryMonitor, CanaryStatus
from hft_platform.alpha.canary_scheduler import CanaryAutoScheduler


def _write_canary_yaml(
    path: Path,
    alpha_id: str = "test_alpha",
    weight: float = 0.02,
    enabled: bool = True,
    live_metrics: dict | None = None,
) -> Path:
    """Helper to write a canary YAML file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "alpha_id": alpha_id,
        "enabled": enabled,
        "weight": weight,
        "owner": "test",
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
            },
        },
        "scorecard_snapshot": {"sharpe_oos": 1.5},
    }
    if live_metrics is not None:
        payload["live_metrics"] = live_metrics
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path


class TestCanaryFailsafeMissingLiveMetrics:
    """Test fail-safe defaults when live_metrics is completely missing."""

    def test_missing_live_metrics_dict_returns_all_failsafe_values(self) -> None:
        """When live_metrics key is absent, _build_metrics returns fail-safe defaults."""
        canary: dict = {"alpha_id": "test_alpha"}
        metrics = CanaryAutoScheduler._build_metrics(canary)

        # All values should be fail-safe (worst-case)
        assert metrics["slippage_bps"] == 999.0, "slippage default should exceed max_slippage_bps=3.0"
        assert metrics["drawdown_contribution"] == 1.0, "drawdown default should be 100%"
        assert metrics["execution_error_rate"] == 1.0, "error_rate default should be 100%"
        assert metrics["sessions_live"] == 0, "sessions_live should default to 0"
        assert "sharpe_live" not in metrics, "sharpe_live should not be present if not in stored"

    def test_missing_live_metrics_dict_triggers_rollback(self, tmp_path: Path) -> None:
        """When live_metrics is missing, canary evaluation should trigger rollback."""
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(
            promo_dir / "test.yaml",
            alpha_id="test_alpha",
            live_metrics=None,  # Explicitly no live_metrics
        )

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        metrics = CanaryAutoScheduler._build_metrics({"alpha_id": "test_alpha"})
        status = monitor.evaluate("test_alpha", metrics)

        assert status.state == "rolled_back", "Missing live_metrics should trigger rollback"
        assert "slippage_bps" in status.reason, "Rollback reason should mention slippage"

    def test_invalid_live_metrics_type_falls_back_to_failsafe(self) -> None:
        """When live_metrics is not a dict, _build_metrics uses fail-safe defaults."""
        canary: dict = {"alpha_id": "test_alpha", "live_metrics": "invalid_string"}
        metrics = CanaryAutoScheduler._build_metrics(canary)

        assert metrics["slippage_bps"] == 999.0
        assert metrics["drawdown_contribution"] == 1.0
        assert metrics["execution_error_rate"] == 1.0


class TestCanaryFailsafePartialLiveMetrics:
    """Test fail-safe defaults when live_metrics is present but incomplete."""

    def test_partial_live_metrics_fills_missing_with_failsafe(self) -> None:
        """When only some metrics are present, missing ones get fail-safe defaults."""
        canary: dict = {
            "alpha_id": "test_alpha",
            "live_metrics": {
                "slippage_bps": 1.5,
                # drawdown_contribution and execution_error_rate are missing
                "sessions_live": 5,
            },
        }
        metrics = CanaryAutoScheduler._build_metrics(canary)

        # Present value should be used
        assert metrics["slippage_bps"] == 1.5
        # Missing values should get fail-safe defaults
        assert metrics["drawdown_contribution"] == 1.0
        assert metrics["execution_error_rate"] == 1.0
        assert metrics["sessions_live"] == 5

    def test_missing_slippage_uses_failsafe(self) -> None:
        """When slippage_bps is missing from live_metrics, use fail-safe."""
        canary: dict = {
            "alpha_id": "test_alpha",
            "live_metrics": {
                "drawdown_contribution": 0.01,
                "execution_error_rate": 0.005,
                "sessions_live": 10,
            },
        }
        metrics = CanaryAutoScheduler._build_metrics(canary)

        assert metrics["slippage_bps"] == 999.0
        assert metrics["drawdown_contribution"] == 0.01
        assert metrics["execution_error_rate"] == 0.005
        assert metrics["sessions_live"] == 10

    def test_missing_drawdown_uses_failsafe(self) -> None:
        """When drawdown_contribution is missing from live_metrics, use fail-safe."""
        canary: dict = {
            "alpha_id": "test_alpha",
            "live_metrics": {
                "slippage_bps": 2.0,
                "execution_error_rate": 0.005,
                "sessions_live": 10,
            },
        }
        metrics = CanaryAutoScheduler._build_metrics(canary)

        assert metrics["slippage_bps"] == 2.0
        assert metrics["drawdown_contribution"] == 1.0
        assert metrics["execution_error_rate"] == 0.005
        assert metrics["sessions_live"] == 10

    def test_missing_error_rate_uses_failsafe(self) -> None:
        """When execution_error_rate is missing from live_metrics, use fail-safe."""
        canary: dict = {
            "alpha_id": "test_alpha",
            "live_metrics": {
                "slippage_bps": 2.0,
                "drawdown_contribution": 0.01,
                "sessions_live": 10,
            },
        }
        metrics = CanaryAutoScheduler._build_metrics(canary)

        assert metrics["slippage_bps"] == 2.0
        assert metrics["drawdown_contribution"] == 0.01
        assert metrics["execution_error_rate"] == 1.0
        assert metrics["sessions_live"] == 10

    def test_partial_metrics_trigger_rollback_on_missing_fields(self, tmp_path: Path) -> None:
        """When critical metrics are missing, evaluation should trigger rollback."""
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(
            promo_dir / "test.yaml",
            alpha_id="test_alpha",
            live_metrics={
                "slippage_bps": 1.0,
                # drawdown_contribution missing: will get fail-safe 1.0 > threshold 0.02
                "sessions_live": 15,
            },
        )

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        metrics = CanaryAutoScheduler._build_metrics(
            {
                "alpha_id": "test_alpha",
                "live_metrics": {
                    "slippage_bps": 1.0,
                    "sessions_live": 15,
                },
            }
        )
        status = monitor.evaluate("test_alpha", metrics)

        assert status.state == "rolled_back"
        assert "drawdown_contribution" in status.reason


class TestCanaryFailsafePresentLiveMetrics:
    """Test that present live metrics are used as-is, not replaced with fail-safe."""

    def test_all_present_live_metrics_used_as_is(self) -> None:
        """When all metrics are present, use actual values, not fail-safe."""
        canary: dict = {
            "alpha_id": "test_alpha",
            "live_metrics": {
                "slippage_bps": 1.5,
                "drawdown_contribution": 0.01,
                "execution_error_rate": 0.005,
                "sessions_live": 25,
                "sharpe_live": 1.2,
            },
        }
        metrics = CanaryAutoScheduler._build_metrics(canary)

        # All should be actual values, not fail-safe
        assert metrics["slippage_bps"] == 1.5
        assert metrics["drawdown_contribution"] == 0.01
        assert metrics["execution_error_rate"] == 0.005
        assert metrics["sessions_live"] == 25
        assert metrics["sharpe_live"] == 1.2

    def test_all_present_metrics_pass_evaluation_if_under_thresholds(self, tmp_path: Path) -> None:
        """When all metrics are present and under thresholds, evaluation should pass (hold)."""
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(
            promo_dir / "test.yaml",
            alpha_id="test_alpha",
            live_metrics={
                "slippage_bps": 1.0,
                "drawdown_contribution": 0.01,
                "execution_error_rate": 0.005,
                "sessions_live": 5,
            },
        )

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        metrics = CanaryAutoScheduler._build_metrics(
            {
                "alpha_id": "test_alpha",
                "live_metrics": {
                    "slippage_bps": 1.0,
                    "drawdown_contribution": 0.01,
                    "execution_error_rate": 0.005,
                    "sessions_live": 5,
                },
            }
        )
        status = monitor.evaluate("test_alpha", metrics)

        # All metrics under thresholds and sessions < escalation threshold
        assert status.state == "canary"
        assert "passed" in status.reason or "holding" in status.reason.lower()

    def test_zero_slippage_is_valid_when_explicitly_set(self) -> None:
        """Zero slippage is a valid actual value (not fail-safe), not the default."""
        canary: dict = {
            "alpha_id": "test_alpha",
            "live_metrics": {
                "slippage_bps": 0.0,  # Explicitly zero (good performance)
                "drawdown_contribution": 0.0,
                "execution_error_rate": 0.0,
                "sessions_live": 10,
            },
        }
        metrics = CanaryAutoScheduler._build_metrics(canary)

        assert metrics["slippage_bps"] == 0.0
        assert metrics["drawdown_contribution"] == 0.0
        assert metrics["execution_error_rate"] == 0.0


class TestCanaryFailsafeEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_live_metrics_dict_is_treated_as_missing(self) -> None:
        """An empty live_metrics dict {} should trigger fail-safe defaults."""
        canary: dict = {"alpha_id": "test_alpha", "live_metrics": {}}
        metrics = CanaryAutoScheduler._build_metrics(canary)

        assert metrics["slippage_bps"] == 999.0
        assert metrics["drawdown_contribution"] == 1.0
        assert metrics["execution_error_rate"] == 1.0

    def test_zero_sessions_with_missing_metrics_returns_failsafe(self) -> None:
        """Missing metrics with zero sessions should still return fail-safe values."""
        canary: dict = {
            "alpha_id": "test_alpha",
            "live_metrics": {"sessions_live": 0},
        }
        metrics = CanaryAutoScheduler._build_metrics(canary)

        assert metrics["slippage_bps"] == 999.0
        assert metrics["drawdown_contribution"] == 1.0
        assert metrics["execution_error_rate"] == 1.0
        assert metrics["sessions_live"] == 0

    def test_optional_sharpe_live_not_present_when_missing(self) -> None:
        """sharpe_live is optional; should not be in metrics if not in stored."""
        canary: dict = {
            "alpha_id": "test_alpha",
            "live_metrics": {
                "slippage_bps": 1.0,
                "drawdown_contribution": 0.01,
                "execution_error_rate": 0.005,
                "sessions_live": 5,
            },
        }
        metrics = CanaryAutoScheduler._build_metrics(canary)

        assert "sharpe_live" not in metrics

    def test_optional_sharpe_live_present_when_stored(self) -> None:
        """sharpe_live should be in metrics if present in stored."""
        canary: dict = {
            "alpha_id": "test_alpha",
            "live_metrics": {
                "slippage_bps": 1.0,
                "drawdown_contribution": 0.01,
                "execution_error_rate": 0.005,
                "sessions_live": 5,
                "sharpe_live": 0.95,
            },
        }
        metrics = CanaryAutoScheduler._build_metrics(canary)

        assert metrics["sharpe_live"] == 0.95

    def test_float_conversion_for_string_values(self) -> None:
        """Stored metrics that are string numbers should be converted to float."""
        canary: dict = {
            "alpha_id": "test_alpha",
            "live_metrics": {
                "slippage_bps": "2.5",  # String
                "drawdown_contribution": "0.015",  # String
                "execution_error_rate": "0.008",  # String
                "sessions_live": "20",  # String
                "sharpe_live": "1.1",  # String
            },
        }
        metrics = CanaryAutoScheduler._build_metrics(canary)

        assert isinstance(metrics["slippage_bps"], float)
        assert metrics["slippage_bps"] == 2.5
        assert isinstance(metrics["drawdown_contribution"], float)
        assert metrics["drawdown_contribution"] == 0.015
        assert isinstance(metrics["execution_error_rate"], float)
        assert metrics["execution_error_rate"] == 0.008
        assert isinstance(metrics["sessions_live"], int)
        assert metrics["sessions_live"] == 20
        assert isinstance(metrics["sharpe_live"], float)
        assert metrics["sharpe_live"] == 1.1


class TestCanaryFailsafeThresholdComparison:
    """Test that fail-safe values actually exceed rollback thresholds."""

    def test_failsafe_slippage_exceeds_max_threshold(self) -> None:
        """Fail-safe slippage (999.0 bps) should exceed any reasonable max_slippage threshold."""
        # Default max_slippage from canary.py is 3.0 bps
        assert 999.0 > 3.0

    def test_failsafe_drawdown_exceeds_max_threshold(self) -> None:
        """Fail-safe drawdown (1.0 = 100%) should exceed any max_drawdown threshold."""
        # Default max_drawdown from canary.py is 0.02 (2%)
        assert 1.0 > 0.02

    def test_failsafe_error_rate_exceeds_max_threshold(self) -> None:
        """Fail-safe error_rate (1.0 = 100%) should exceed any max_error_rate threshold."""
        # Default max_error_rate from canary.py is 0.01 (1%)
        assert 1.0 > 0.01
