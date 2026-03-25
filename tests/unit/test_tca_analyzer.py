"""TCAAnalyzer unit tests."""
from hft_platform.tca.analyzer import TCAAnalyzer
from hft_platform.tca.types import SlippageBreakdown


def _breakdown(total_bps: float) -> SlippageBreakdown:
    return SlippageBreakdown(
        commission_bps=0.5, tax_bps=0.3,
        delay_cost_bps=total_bps * 0.3, execution_cost_bps=total_bps * 0.4,
        market_impact_bps=total_bps * 0.1, total_bps=total_bps,
    )


def test_aggregate_mean_and_p95() -> None:
    analyzer = TCAAnalyzer()
    breakdowns = [_breakdown(float(i)) for i in range(1, 101)]
    report = analyzer.aggregate(breakdowns, date="2026-03-25", strategy="s1",
                                symbol="XMT", volume=100, notional=10_000_000)
    assert report.trade_count == 100
    assert abs(report.total_cost_bps_mean - 50.5) < 0.1
    assert report.total_cost_bps_p95 >= 95.0


def test_empty_breakdowns() -> None:
    analyzer = TCAAnalyzer()
    report = analyzer.aggregate([], date="2026-03-25", strategy="s1",
                                symbol="XMT", volume=0, notional=0)
    assert report.trade_count == 0
    assert report.total_cost_bps_mean == 0
