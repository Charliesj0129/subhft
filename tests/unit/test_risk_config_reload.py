"""Tests for risk config hot-reload (WU-10)."""

import asyncio
import os
import tempfile
from unittest.mock import MagicMock, patch

import yaml


class TestRiskConfigReload:
    def _make_risk_engine(self, config=None):
        config = config or {
            "global_defaults": {"max_price_cap": 5000.0, "tick_size": 0.01, "price_band_ticks": 20},
            "strategies": {},
        }
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        yaml.dump(config, tmp)
        tmp.close()
        with (
            patch("hft_platform.risk.engine.MetricsRegistry") as m,
            patch("hft_platform.risk.engine.LatencyRecorder") as lr,
            patch("hft_platform.risk.engine.get_audit_writer"),
        ):
            m.get.return_value = MagicMock()
            lr.get.return_value = MagicMock()
            from hft_platform.risk.engine import RiskEngine

            engine = RiskEngine(tmp.name, asyncio.Queue(), asyncio.Queue())
            return engine, tmp.name

    def test_reload_config_updates(self):
        engine, path = self._make_risk_engine()
        new_config = {"global_defaults": {"max_price_cap": 10000.0}, "strategies": {"s1": {}}}
        with open(path, "w") as f:
            yaml.dump(new_config, f)
        engine.reload_config()
        assert engine.config["global_defaults"]["max_price_cap"] == 10000.0
        os.unlink(path)

    def test_storm_guard_reload_thresholds(self):
        from hft_platform.risk.storm_guard import RiskThresholds, StormGuard

        guard = StormGuard(thresholds=RiskThresholds(warm_drawdown_bps=-50, halt_drawdown_bps=-200))
        guard.reload_thresholds({"risk": {"warm_drawdown_bps": -100, "halt_drawdown_bps": -500}})
        assert guard.thresholds.warm_drawdown_bps == -100
        assert guard.thresholds.halt_drawdown_bps == -500
