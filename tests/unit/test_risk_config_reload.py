"""Tests for risk config hot-reload via SIGHUP (WU-10)."""

import asyncio
import os
import tempfile
from unittest.mock import MagicMock, patch

import yaml


class TestRiskConfigReload:
    """WU-10: Verify risk config hot-reload."""

    def _make_risk_engine(self, config: dict | None = None):
        config = config or {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "tick_size": 0.01,
                "price_band_ticks": 20,
            },
            "strategies": {},
        }

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        yaml.dump(config, tmp)
        tmp.close()

        with (
            patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
            patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
            patch("hft_platform.recorder.audit.get_audit_writer"),
        ):
            mock_mr.get.return_value = MagicMock()
            mock_lr.get.return_value = MagicMock()

            from hft_platform.risk.engine import RiskEngine

            engine = RiskEngine(
                config_path=tmp.name,
                intent_queue=asyncio.Queue(),
                order_queue=asyncio.Queue(),
            )
            return engine, tmp.name

    def test_reload_config_updates_validators(self):
        engine, config_path = self._make_risk_engine()

        new_config = {
            "global_defaults": {
                "max_price_cap": 10000.0,
                "tick_size": 0.05,
                "price_band_ticks": 50,
            },
            "strategies": {"test_strat": {"price_band_ticks": 100}},
        }
        with open(config_path, "w") as f:
            yaml.dump(new_config, f)

        engine.reload_config()

        assert engine.config["global_defaults"]["max_price_cap"] == 10000.0
        assert "test_strat" in engine.config.get("strategies", {})

        os.unlink(config_path)

    def test_reload_config_handles_file_error(self):
        engine, config_path = self._make_risk_engine()
        os.unlink(config_path)

        # Should not raise -- old config preserved
        engine.reload_config()

        assert "global_defaults" in engine.config

    def test_storm_guard_reload_thresholds(self):
        from hft_platform.risk.storm_guard import RiskThresholds, StormGuard

        guard = StormGuard(
            thresholds=RiskThresholds(
                warm_drawdown_bps=-50,
                halt_drawdown_bps=-200,
            )
        )

        new_config = {
            "risk": {
                "warm_drawdown_bps": -100,
                "halt_drawdown_bps": -500,
            }
        }
        guard.reload_thresholds(new_config)

        assert guard.thresholds.warm_drawdown_bps == -100
        assert guard.thresholds.halt_drawdown_bps == -500
