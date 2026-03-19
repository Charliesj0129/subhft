"""Tests for strict price mode enforcement (WU-06)."""

import asyncio
from unittest.mock import MagicMock, patch


class TestRiskEngineFloatPriceReject:
    def test_risk_engine_rejects_float_price(self):
        with (
            patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
            patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
            patch("hft_platform.risk.engine.get_audit_writer"),
            patch("builtins.open", MagicMock()),
            patch("hft_platform.risk.engine.yaml") as mock_yaml,
        ):
            mock_mr.get.return_value = MagicMock()
            mock_lr.get.return_value = MagicMock()
            mock_yaml.safe_load.return_value = {"global_defaults": {}, "strategies": {}}
            from hft_platform.risk.engine import RiskEngine

            engine = RiskEngine("config/base/strategy_limits.yaml", asyncio.Queue(), asyncio.Queue())
            intent = MagicMock()
            intent.price = 100.5
            intent.qty = 1
            intent.intent_type = 0
            intent.strategy_id = "test"
            intent.symbol = "2330"
            intent.trace_id = ""
            decision = engine.evaluate(intent)
            assert not decision.approved
            assert decision.reason_code == "FLOAT_PRICE"
