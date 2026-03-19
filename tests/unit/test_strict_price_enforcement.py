"""Tests for strict price mode enforcement (WU-06)."""
import asyncio
import os
import tempfile
from unittest.mock import MagicMock, patch

import yaml


class TestFloatReject:
    def test_rejects_float(self):
        """Risk engine evaluate() rejects float prices with FLOAT_PRICE."""
        config = {"global_defaults": {"max_price_cap": 5000.0, "tick_size": 0.01, "price_band_ticks": 20}, "strategies": {}}
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
            e = RiskEngine(tmp.name, asyncio.Queue(), asyncio.Queue())
            intent = MagicMock(price=100.5, qty=1, intent_type=0, strategy_id="t", symbol="2330", trace_id="")
            d = e.evaluate(intent)
            assert not d.approved
            assert d.reason_code == "FLOAT_PRICE"
        os.unlink(tmp.name)
