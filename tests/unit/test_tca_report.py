"""Tests for TCA attribution engine."""
import pytest
from hft_platform.cli._tca import TCAEngine, TradeAttribution


class TestTradeAttribution:
    def test_gross_alpha_computation(self):
        attr = TradeAttribution(fill_pnl_ntd=80, slippage_ntd=20, fees_ntd=12)
        assert attr.gross_alpha_ntd == 100  # 80 + 20
        assert attr.net_alpha_ntd == 68     # 100 - 20 - 12

    def test_retention_rate(self):
        attr = TradeAttribution(fill_pnl_ntd=80, slippage_ntd=20, fees_ntd=12)
        assert attr.retention_rate == pytest.approx(0.68)

    def test_retention_rate_zero_gross(self):
        attr = TradeAttribution(fill_pnl_ntd=-20, slippage_ntd=20, fees_ntd=0)
        assert attr.retention_rate == 0.0


class TestTCAEngine:
    def test_aggregate_by_hour(self):
        records = [
            {"hour_of_day": 9, "slippage_ticks": 2},
            {"hour_of_day": 9, "slippage_ticks": 1},
            {"hour_of_day": 10, "slippage_ticks": 3},
        ]
        by_hour = TCAEngine.aggregate_by_dimension(records, "hour_of_day", "slippage_ticks")
        assert by_hour[9] == pytest.approx(1.5)
        assert by_hour[10] == pytest.approx(3.0)
