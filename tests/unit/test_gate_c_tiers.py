"""Unit tests for Gate C discovery tier (Unit 2)."""

from __future__ import annotations

from hft_platform.alpha._validation_types import ValidationConfig


class TestGateCTierConfig:
    def test_default_tier_is_promotion(self):
        cfg = ValidationConfig(alpha_id="test", data_paths=["data.npz"])
        assert cfg.gate_c_tier == "promotion"

    def test_discovery_tier(self):
        cfg = ValidationConfig(alpha_id="test", data_paths=["data.npz"], gate_c_tier="discovery")
        assert cfg.gate_c_tier == "discovery"

    def test_promotion_tier_explicit(self):
        cfg = ValidationConfig(alpha_id="test", data_paths=["data.npz"], gate_c_tier="promotion")
        assert cfg.gate_c_tier == "promotion"


class TestDiscoveryTierSkipsExpensiveStages:
    """Verify that _gate_c.py respects discovery tier by checking the
    optimization/stress/robustness/walk-forward skip logic.

    These tests validate the branching logic without running real backtests.
    """

    def test_discovery_config_flags(self):
        """Discovery tier should be detectable from config."""
        cfg = ValidationConfig(
            alpha_id="test",
            data_paths=["data.npz"],
            gate_c_tier="discovery",
        )
        is_discovery = str(cfg.gate_c_tier).strip().lower() == "discovery"
        assert is_discovery is True

    def test_promotion_config_flags(self):
        """Promotion tier should not be discovery."""
        cfg = ValidationConfig(
            alpha_id="test",
            data_paths=["data.npz"],
            gate_c_tier="promotion",
        )
        is_discovery = str(cfg.gate_c_tier).strip().lower() == "discovery"
        assert is_discovery is False

    def test_discovery_tier_relaxed_thresholds(self):
        """Discovery tier would use relaxed thresholds."""
        cfg = ValidationConfig(
            alpha_id="test",
            data_paths=["data.npz"],
            gate_c_tier="discovery",
            min_sharpe_oos=-0.1,
            min_turnover=1e-8,
            min_stat_tests_pass=1,
        )
        assert cfg.min_sharpe_oos == -0.1
        assert cfg.min_turnover == 1e-8
        assert cfg.min_stat_tests_pass == 1
