import asyncio
from unittest.mock import MagicMock, patch


class TestFloatReject:
    def test_rejects_float(self):
        with (
            patch("hft_platform.risk.engine.MetricsRegistry") as m,
            patch("hft_platform.risk.engine.LatencyRecorder") as l,
            patch("hft_platform.risk.engine.get_audit_writer"),
            patch("builtins.open", MagicMock()),
            patch("hft_platform.risk.engine.yaml") as y,
        ):
            m.get.return_value = MagicMock()
            l.get.return_value = MagicMock()
            y.safe_load.return_value = {"global_defaults": {}, "strategies": {}}
            from hft_platform.risk.engine import RiskEngine

            e = RiskEngine("x.yaml", asyncio.Queue(), asyncio.Queue())
            i = MagicMock(price=100.5, qty=1, intent_type=0, strategy_id="t", symbol="2330", trace_id="")
            d = e.evaluate(i)
            assert not d.approved
            assert d.reason_code == "FLOAT_PRICE"
