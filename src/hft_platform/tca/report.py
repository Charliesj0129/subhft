"""Daily TCA report generator — reads from hft.trades, writes to hft.tca_daily + JSON."""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import structlog

from hft_platform.contracts.strategy import Side as SideEnum
from hft_platform.tca.analyzer import TCAAnalyzer
from hft_platform.tca.impact import SqrtImpactModel
from hft_platform.tca.slippage import SlippageDecomposer
from hft_platform.tca.types import TCADailyReport

logger = structlog.get_logger(__name__)

_FETCH_QUERY = """
SELECT strategy_id, symbol, side, price_scaled, qty, fee_scaled, tax_scaled,
       decision_price_scaled, arrival_price_scaled, gross_pnl_scaled, match_ts
FROM hft.trades
WHERE toDate(toDateTime(match_ts / 1000000000)) = {date:String}
  AND decision_price_scaled > 0
ORDER BY match_ts
"""


class TCAReportGenerator:
    __slots__ = ("_ch_client", "_decomposer", "_impact_model", "_analyzer", "_output_dir")

    def __init__(self, ch_client: Any, output_dir: str = "reports/tca", impact_eta: float = 1.0) -> None:
        self._ch_client = ch_client
        self._decomposer = SlippageDecomposer()
        self._impact_model = SqrtImpactModel(eta=impact_eta)
        self._analyzer = TCAAnalyzer()
        self._output_dir = Path(output_dir)

    async def generate_daily(self, date: str) -> list[TCADailyReport]:
        rows = await asyncio.to_thread(self._ch_client.execute, _FETCH_QUERY, {"date": date})
        if not rows:
            logger.info("tca_no_fills", date=date)
            return []

        groups: dict[tuple[str, str], list] = defaultdict(list)
        for row in rows:
            groups[(row[0], row[1])].append(row)

        reports: list[TCADailyReport] = []
        for (strategy, symbol), fills in groups.items():
            breakdowns = []
            total_volume = 0
            total_notional = 0
            for row in fills:
                _, _, side, price, qty, fee, tax, dec_p, arr_p, gross, _ = row
                total_volume += qty
                notional_ntd = (price / 10_000) * qty
                total_notional += int(notional_ntd)
                fill_ns = SimpleNamespace(
                    side=SideEnum.SELL if side == "sell" else SideEnum.BUY,
                    price=price, fee=fee, tax=tax,
                    decision_price=dec_p, arrival_price=arr_p,
                )
                bd = self._decomposer.decompose(fill_ns, notional_ntd)
                breakdowns.append(bd)
            report = self._analyzer.aggregate(
                breakdowns, date=date, strategy=strategy,
                symbol=symbol, volume=total_volume, notional=total_notional,
            )
            reports.append(report)

        await self._write_to_clickhouse(reports)
        self._write_json(date, reports)
        logger.info("tca_report_generated", date=date, count=len(reports))
        return reports

    async def _write_to_clickhouse(self, reports: list[TCADailyReport]) -> None:
        if not reports:
            return
        insert_sql = """INSERT INTO hft.tca_daily (
            date, strategy, symbol, trade_count, volume, notional,
            commission_bps_mean, tax_bps_mean, delay_cost_bps_mean, delay_cost_bps_p95,
            exec_cost_bps_mean, exec_cost_bps_p95, impact_bps_mean,
            total_cost_bps_mean, total_cost_bps_p95
        ) VALUES"""
        rows = [
            (r.date, r.strategy, r.symbol, r.trade_count, r.volume, r.notional,
             r.commission_bps_mean, r.tax_bps_mean, r.delay_cost_bps_mean, r.delay_cost_bps_p95,
             r.exec_cost_bps_mean, r.exec_cost_bps_p95, r.impact_bps_mean,
             r.total_cost_bps_mean, r.total_cost_bps_p95)
            for r in reports
        ]
        await asyncio.to_thread(self._ch_client.execute, insert_sql, rows)

    def _write_json(self, date: str, reports: list[TCADailyReport]) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        path = self._output_dir / f"{date}.json"
        path.write_text(json.dumps([asdict(r) for r in reports], indent=2, default=str))
