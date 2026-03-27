"""Tests for TCAReportGenerator."""
from __future__ import annotations

import pytest

from hft_platform.tca.report import TCAReportGenerator
from hft_platform.tca.types import TCADailyReport


def _make_report(strategy: str = "CBS_TMFD6", symbol: str = "TMFD6", trade_count: int = 10) -> TCADailyReport:
    return TCADailyReport(
        date="2026-03-27",
        strategy=strategy,
        symbol=symbol,
        trade_count=trade_count,
        volume=trade_count * 5,
        notional=trade_count * 100_000,
        commission_bps_mean=1.2,
        tax_bps_mean=0.6,
        delay_cost_bps_mean=0.3,
        delay_cost_bps_p95=0.8,
        exec_cost_bps_mean=0.5,
        exec_cost_bps_p95=1.2,
        impact_bps_mean=0.0,
        total_cost_bps_mean=2.6,
        total_cost_bps_p95=3.5,
    )


class TestTCAReportGenerator:
    def test_format_empty_reports_returns_empty_string(self) -> None:
        gen = TCAReportGenerator()
        msg = gen.format_telegram_section([])
        assert msg == ""

    def test_format_single_report_contains_strategy(self) -> None:
        gen = TCAReportGenerator()
        report = _make_report()
        msg = gen.format_telegram_section([report])
        assert "CBS_TMFD6" in msg
        assert "TMFD6" in msg
        assert "10" in msg

    def test_format_single_report_contains_cost_fields(self) -> None:
        gen = TCAReportGenerator()
        report = _make_report()
        msg = gen.format_telegram_section([report])
        # total_cost_bps_mean=2.6
        assert "2.6" in msg
        # commission_bps_mean=1.2
        assert "1.2" in msg

    def test_format_single_report_has_tca_summary_header(self) -> None:
        gen = TCAReportGenerator()
        report = _make_report()
        msg = gen.format_telegram_section([report])
        assert "TCA Summary" in msg

    def test_format_preserves_all_reports(self) -> None:
        gen = TCAReportGenerator()
        reports = [
            _make_report(strategy=f"s{i}", trade_count=i)
            for i in range(1, 4)
        ]
        msg = gen.format_telegram_section(reports)
        assert "s1" in msg
        assert "s2" in msg
        assert "s3" in msg

    def test_format_multiple_reports_has_single_header(self) -> None:
        gen = TCAReportGenerator()
        reports = [_make_report(strategy=f"strat{i}") for i in range(3)]
        msg = gen.format_telegram_section(reports)
        assert msg.count("TCA Summary") == 1

    def test_format_report_volume_shown(self) -> None:
        gen = TCAReportGenerator()
        report = _make_report(trade_count=7)
        msg = gen.format_telegram_section([report])
        # volume = 7 * 5 = 35
        assert "35" in msg

    def test_format_single_report_matches_spec(self) -> None:
        gen = TCAReportGenerator()
        report = TCADailyReport(
            date="2026-03-27", strategy="CBS_TMFD6", symbol="TMFD6",
            trade_count=10, volume=50, notional=500_000,
            commission_bps_mean=1.2, tax_bps_mean=0.6,
            delay_cost_bps_mean=0.3, delay_cost_bps_p95=0.8,
            exec_cost_bps_mean=0.5, exec_cost_bps_p95=1.2,
            impact_bps_mean=0.0, total_cost_bps_mean=2.6, total_cost_bps_p95=3.5,
        )
        msg = gen.format_telegram_section([report])
        assert "CBS_TMFD6" in msg
        assert "TMFD6" in msg
        assert "10" in msg
