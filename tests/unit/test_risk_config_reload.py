import asyncio
import os
import tempfile
from unittest.mock import MagicMock, patch

import yaml


class TestReload:
    def test_reload(self):
        cfg = {
            "global_defaults": {"max_price_cap": 5000.0, "tick_size": 0.01, "price_band_ticks": 20},
            "strategies": {},
        }
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        yaml.dump(cfg, tmp)
        tmp.close()
        with (
            patch("hft_platform.risk.engine.MetricsRegistry") as m,
            patch("hft_platform.risk.engine.LatencyRecorder") as l,
            patch("hft_platform.risk.engine.get_audit_writer"),
        ):
            m.get.return_value = MagicMock()
            l.get.return_value = MagicMock()
            from hft_platform.risk.engine import RiskEngine

            e = RiskEngine(tmp.name, asyncio.Queue(), asyncio.Queue())
        new_cfg = {"global_defaults": {"max_price_cap": 10000.0}, "strategies": {"s": {}}}
        with open(tmp.name, "w") as f:
            yaml.dump(new_cfg, f)
        e.reload_config()
        assert e.config["global_defaults"]["max_price_cap"] == 10000.0
        os.unlink(tmp.name)

    def test_storm_reload(self):
        from hft_platform.risk.storm_guard import RiskThresholds, StormGuard

        g = StormGuard(thresholds=RiskThresholds(warm_drawdown_bps=-50, halt_drawdown_bps=-200))
        g.reload_thresholds({"risk": {"warm_drawdown_bps": -100, "halt_drawdown_bps": -500}})
        assert g.thresholds.warm_drawdown_bps == -100
        assert g.thresholds.halt_drawdown_bps == -500
