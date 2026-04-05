"""TCA Analyzer — queries ClickHouse hft.fills for daily cost reporting."""

from __future__ import annotations

from typing import Any

import structlog

from hft_platform.tca.types import TCADailyReport

logger = structlog.get_logger(__name__)

_DAILY_QUERY = """\
SELECT
    strategy_id,
    symbol,
    count(*)        AS trade_count,
    sum(qty)        AS total_qty,
    sum(toInt64(price_scaled) * toInt64(qty)) AS sum_notional_scaled,
    sum(fee_scaled) AS total_fee_scaled,
    sum(tax_scaled) AS total_tax_scaled
FROM hft.fills
WHERE toDate(ts_exchange / 1000000000) = %(date)s
GROUP BY strategy_id, symbol
ORDER BY strategy_id, symbol
"""


class TCAAnalyzer:
    """Queries ClickHouse hft.fills and produces TCADailyReport aggregates."""

    __slots__ = ("_ch_client", "_point_value_map", "_symbol_to_product")

    def __init__(
        self,
        ch_client: Any,
        *,
        point_value_map: dict[str, int] | None = None,
        symbol_to_product: dict[str, str] | None = None,
    ) -> None:
        self._ch_client = ch_client
        # Maps product code (e.g. "TX", "MTX", "XMT") → point value (NTD per point).
        self._point_value_map: dict[str, int] = point_value_map or {}
        # Optional: maps symbol (e.g. "TXFD6") → product code (e.g. "TX").
        self._symbol_to_product: dict[str, str] = symbol_to_product or {}

    def daily_report(self, date_str: str) -> list[TCADailyReport]:
        """Return per-(strategy, symbol) cost reports for a given date.

        On ClickHouse failure, returns an empty list and logs a warning.
        """
        try:
            rows = self._ch_client.execute(_DAILY_QUERY, {"date": date_str})
        except Exception:
            logger.warning("tca_daily_query_failed", date=date_str, exc_info=True)
            return []

        reports: list[TCADailyReport] = []
        for row in rows:
            strategy_id, symbol, trade_count, total_qty, sum_notional_scaled, total_fee_scaled, total_tax_scaled = row

            # Resolve point_value for this symbol.
            # SQL gives price_scaled * qty; multiply by point_value to get true NTD notional.
            product = self._symbol_to_product.get(symbol, symbol)
            point_value = self._point_value_map.get(product, 1)
            if point_value == 1 and (self._point_value_map or self._symbol_to_product):
                logger.warning(
                    "tca_unknown_symbol_point_value",
                    symbol=symbol,
                    product=product,
                    defaulting_to=1,
                )

            # All scaled values are x10000. Apply point_value then convert to real NTD.
            corrected_notional_scaled = sum_notional_scaled * point_value if sum_notional_scaled else 0
            notional_real = corrected_notional_scaled / 10000.0 if corrected_notional_scaled else 0.0

            # fee_scaled is combined (commission + tax); tax_scaled is tax only
            # commission = fee - tax
            tax_real = total_tax_scaled / 10000.0 if total_tax_scaled else 0.0
            commission_real = (total_fee_scaled / 10000.0 - tax_real) if total_fee_scaled else 0.0

            if notional_real > 0:
                commission_bps = (commission_real / notional_real) * 10000.0
                tax_bps = (tax_real / notional_real) * 10000.0
                total_cost_bps = commission_bps + tax_bps
            else:
                commission_bps = 0.0
                tax_bps = 0.0
                total_cost_bps = 0.0

            reports.append(
                TCADailyReport(
                    date=date_str,
                    strategy=strategy_id,
                    symbol=symbol,
                    trade_count=trade_count,
                    volume=total_qty,
                    notional=corrected_notional_scaled,
                    commission_bps_mean=commission_bps,
                    tax_bps_mean=tax_bps,
                    delay_cost_bps_mean=0.0,
                    delay_cost_bps_p95=0.0,
                    exec_cost_bps_mean=0.0,
                    exec_cost_bps_p95=0.0,
                    impact_bps_mean=0.0,
                    total_cost_bps_mean=total_cost_bps,
                    total_cost_bps_p95=0.0,
                )
            )
        return reports
