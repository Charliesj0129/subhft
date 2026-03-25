"""Tests for daily PnL report generation."""
import pytest
from hft_platform.ops.daily_pnl_report import DailyPnLReport, DailyReportData


class TestDailyReportData:
    def test_net_pnl_calculation(self):
        data = DailyReportData(
            report_date="2026-03-24",
            realized_pnl_ntd=300, fees_ntd=46, tax_ntd=20,
        )
        assert data.net_pnl_ntd == 234  # 300 - 46 - 20

    def test_win_rate(self):
        data = DailyReportData(win_count=7, loss_count=5)
        assert data.win_rate == pytest.approx(0.583, abs=0.01)

    def test_win_rate_no_trades(self):
        data = DailyReportData(win_count=0, loss_count=0)
        assert data.win_rate == 0.0

    def test_profit_factor(self):
        data = DailyReportData(gross_profit_ntd=500, gross_loss_ntd=200)
        assert data.profit_factor == pytest.approx(2.5)

    def test_profit_factor_no_losses(self):
        data = DailyReportData(gross_profit_ntd=500, gross_loss_ntd=0)
        assert data.profit_factor == float("inf")


class TestTelegramFormat:
    def test_format_daily_summary(self):
        data = DailyReportData(
            report_date="2026-03-24",
            strategy_id="MM_ALPHA_V1",
            symbol="TMFD6",
            realized_pnl_ntd=254, unrealized_pnl_ntd=0,
            fees_ntd=46, tax_ntd=20,
            orders_sent=12, orders_filled=12, orders_cancelled=0,
            avg_slippage_ticks=-0.8, slippage_cost_ntd=-96,
            peak_pnl_ntd=480, max_drawdown_ntd=-160,
            soft_limit_triggers=0, hard_limit_triggers=0,
            autonomy_transitions=0,
            win_count=7, loss_count=5,
            gross_profit_ntd=400, gross_loss_ntd=146,
        )
        msg = DailyPnLReport.format_telegram(data)
        assert "Daily Summary" in msg
        assert "254" in msg or "+254" in msg
        assert "12 sent" in msg
        assert "Peak PnL" in msg
        assert "Cumulative" not in msg  # cumulative needs separate data
