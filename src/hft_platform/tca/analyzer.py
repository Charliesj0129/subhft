"""Aggregate SlippageBreakdown records into TCADailyReport."""
from __future__ import annotations

import statistics

from hft_platform.tca.types import SlippageBreakdown, TCADailyReport


def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = min(int(len(sorted_data) * pct / 100), len(sorted_data) - 1)
    return sorted_data[idx]


class TCAAnalyzer:
    __slots__ = ()

    def aggregate(
        self, breakdowns: list[SlippageBreakdown],
        date: str, strategy: str, symbol: str, volume: int, notional: int,
    ) -> TCADailyReport:
        if not breakdowns:
            return TCADailyReport(
                date=date, strategy=strategy, symbol=symbol,
                trade_count=0, volume=volume, notional=notional,
                commission_bps_mean=0, tax_bps_mean=0,
                delay_cost_bps_mean=0, delay_cost_bps_p95=0,
                exec_cost_bps_mean=0, exec_cost_bps_p95=0,
                impact_bps_mean=0, total_cost_bps_mean=0, total_cost_bps_p95=0,
            )
        return TCADailyReport(
            date=date, strategy=strategy, symbol=symbol,
            trade_count=len(breakdowns), volume=volume, notional=notional,
            commission_bps_mean=statistics.mean(b.commission_bps for b in breakdowns),
            tax_bps_mean=statistics.mean(b.tax_bps for b in breakdowns),
            delay_cost_bps_mean=statistics.mean(b.delay_cost_bps for b in breakdowns),
            delay_cost_bps_p95=_percentile([b.delay_cost_bps for b in breakdowns], 95),
            exec_cost_bps_mean=statistics.mean(b.execution_cost_bps for b in breakdowns),
            exec_cost_bps_p95=_percentile([b.execution_cost_bps for b in breakdowns], 95),
            impact_bps_mean=statistics.mean(b.market_impact_bps for b in breakdowns),
            total_cost_bps_mean=statistics.mean(b.total_bps for b in breakdowns),
            total_cost_bps_p95=_percentile([b.total_bps for b in breakdowns], 95),
        )
