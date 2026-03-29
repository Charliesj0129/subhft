"""Tests for strict price mode enforcement (WU-06)."""

import asyncio
from unittest.mock import MagicMock, mock_open, patch


class TestFloatReject:
    def test_rejects_float(self):
        """Risk engine evaluate() rejects float prices with FLOAT_PRICE."""
        with (
            patch("hft_platform.risk.engine.MetricsRegistry") as m,
            patch("hft_platform.risk.engine.LatencyRecorder") as lr,
            patch("hft_platform.recorder.audit.get_audit_writer"),
            patch("builtins.open", mock_open(read_data="")),
            patch("hft_platform.risk.engine.yaml") as y,
            patch(
                "hft_platform.risk.validators.SymbolMetadataPriceScaleProvider",
                return_value=MagicMock(get_scale=MagicMock(return_value=10000)),
            ),
        ):
            m.get.return_value = MagicMock()
            lr.get.return_value = MagicMock()
            y.safe_load.return_value = {"global_defaults": {}, "strategies": {}}
            from hft_platform.risk.engine import RiskEngine

            e = RiskEngine("x.yaml", asyncio.Queue(), asyncio.Queue())
            i = MagicMock(price=100.5, qty=1, intent_type=0, strategy_id="t", symbol="2330", trace_id="")
            d = e.evaluate(i)
            assert not d.approved
            assert d.reason_code == "FLOAT_PRICE"
