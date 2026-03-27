"""Tests for DailyPnlSection."""

from __future__ import annotations

from hft_platform.ops.daily_pnl_report import DailyPnlSection


class TestDailyPnlSection:
    def test_format_positive_pnl_contains_realized(self) -> None:
        section = DailyPnlSection()
        msg = section.format_telegram_section(
            realized_pnl_ntd=5000,
            unrealized_pnl_ntd=1200,
            trade_count=15,
            fill_count=20,
        )
        assert "5,000" in msg
        assert "15" in msg

    def test_format_negative_pnl_contains_value(self) -> None:
        section = DailyPnlSection()
        msg = section.format_telegram_section(
            realized_pnl_ntd=-3000,
            unrealized_pnl_ntd=0,
            trade_count=5,
            fill_count=5,
        )
        assert "-3,000" in msg

    def test_format_zero_trades(self) -> None:
        section = DailyPnlSection()
        msg = section.format_telegram_section(
            realized_pnl_ntd=0,
            unrealized_pnl_ntd=0,
            trade_count=0,
            fill_count=0,
        )
        assert "0" in msg

    def test_format_positive_total_uses_up_icon(self) -> None:
        section = DailyPnlSection()
        msg = section.format_telegram_section(
            realized_pnl_ntd=1000,
            unrealized_pnl_ntd=0,
            trade_count=1,
            fill_count=1,
        )
        # U+1F4C8 = chart with upwards trend
        assert "\U0001f4c8" in msg

    def test_format_negative_total_uses_down_icon(self) -> None:
        section = DailyPnlSection()
        msg = section.format_telegram_section(
            realized_pnl_ntd=-1000,
            unrealized_pnl_ntd=0,
            trade_count=1,
            fill_count=1,
        )
        # U+1F4C9 = chart with downwards trend
        assert "\U0001f4c9" in msg

    def test_format_zero_total_uses_up_icon(self) -> None:
        section = DailyPnlSection()
        msg = section.format_telegram_section(
            realized_pnl_ntd=0,
            unrealized_pnl_ntd=0,
            trade_count=0,
            fill_count=0,
        )
        assert "\U0001f4c8" in msg

    def test_format_total_is_sum_of_realized_and_unrealized(self) -> None:
        section = DailyPnlSection()
        msg = section.format_telegram_section(
            realized_pnl_ntd=3000,
            unrealized_pnl_ntd=2000,
            trade_count=5,
            fill_count=5,
        )
        # total = 5000
        assert "5,000" in msg

    def test_format_fill_count_present(self) -> None:
        section = DailyPnlSection()
        msg = section.format_telegram_section(
            realized_pnl_ntd=0,
            unrealized_pnl_ntd=0,
            trade_count=10,
            fill_count=12,
        )
        assert "12" in msg

    def test_format_contains_daily_pnl_header(self) -> None:
        section = DailyPnlSection()
        msg = section.format_telegram_section(
            realized_pnl_ntd=0,
            unrealized_pnl_ntd=0,
            trade_count=0,
            fill_count=0,
        )
        assert "Daily PnL" in msg
