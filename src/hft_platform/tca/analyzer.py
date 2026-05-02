"""TCA Analyzer — queries ClickHouse hft.fills for daily cost reporting."""

from __future__ import annotations

import math
from typing import Any

import structlog

from hft_platform.tca.types import TCADailyReport

logger = structlog.get_logger(__name__)


def _safe_float(value: object) -> float:
    """Coerce a ClickHouse aggregate result to float, returning 0.0 for None/NaN/Inf."""
    if value is None:
        return 0.0
    f = float(value)  # type: ignore[arg-type]
    if math.isnan(f) or math.isinf(f):
        return 0.0
    return f


def _default_fee_yaml_path() -> str:
    """Return the default path to config/base/fees/futures.yaml."""
    import os

    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    return os.path.join(base, "config", "base", "fees", "futures.yaml")


def load_point_value_config(yaml_path: str) -> tuple[dict[str, int], dict[str, str]]:
    """Load point_value_map and symbol_to_product from fee YAML.

    Returns:
        (point_value_map, symbol_to_product) — both dicts, empty on failure.
    """
    import yaml

    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
    except Exception:
        logger.warning("tca_point_value_config_load_failed", path=yaml_path, exc_info=True)
        return {}, {}

    futures = data.get("futures", {})
    pv_map: dict[str, int] = {}
    for product, cfg in futures.items():
        if product in ("overrides",) or not isinstance(cfg, dict):
            continue
        pv_map[product] = int(cfg.get("point_value", 1))

    sym_map: dict[str, str] = data.get("symbol_map", {})
    return pv_map, sym_map


_DAILY_QUERY = """\
SELECT
    strategy_id,
    symbol,
    count(*)        AS trade_count,
    sum(qty)        AS total_qty,
    sum(toInt64(price_scaled) * toInt64(qty)) AS sum_notional_scaled,
    sum(fee_scaled) AS total_fee_scaled,
    sum(tax_scaled) AS total_tax_scaled,
    -- Per-fill TCA: only fills with decision_price > 0 OR arrival_price > 0
    -- side_sign: +1 for BUY, -1 for SELL
    -- delay_cost_bps per fill = (arrival_price - decision_price) * side_sign / price_scaled * 10000
    -- exec_cost_bps per fill  = (price_scaled - arrival_price)  * side_sign / price_scaled * 10000
    avgIf(
        toFloat64(arrival_price - decision_price)
        * if(side = 'BUY', 1, -1)
        / toFloat64(price_scaled) * 10000,
        decision_price > 0 AND arrival_price > 0 AND price_scaled > 0
    ) AS delay_cost_bps_mean,
    quantileIf(0.95)(
        toFloat64(arrival_price - decision_price)
        * if(side = 'BUY', 1, -1)
        / toFloat64(price_scaled) * 10000,
        decision_price > 0 AND arrival_price > 0 AND price_scaled > 0
    ) AS delay_cost_bps_p95,
    avgIf(
        toFloat64(price_scaled - arrival_price)
        * if(side = 'BUY', 1, -1)
        / toFloat64(price_scaled) * 10000,
        decision_price > 0 AND arrival_price > 0 AND price_scaled > 0
    ) AS exec_cost_bps_mean,
    quantileIf(0.95)(
        toFloat64(price_scaled - arrival_price)
        * if(side = 'BUY', 1, -1)
        / toFloat64(price_scaled) * 10000,
        decision_price > 0 AND arrival_price > 0 AND price_scaled > 0
    ) AS exec_cost_bps_p95
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
            (
                strategy_id,
                symbol,
                trade_count,
                total_qty,
                sum_notional_scaled,
                total_fee_scaled,
                total_tax_scaled,
                delay_mean,
                delay_p95,
                exec_mean,
                exec_p95,
            ) = row

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
            else:
                commission_bps = 0.0
                tax_bps = 0.0

            # Coerce TCA aggregates — ClickHouse returns NaN/None when no
            # qualifying fills exist (all decision_price=0 AND arrival_price=0).
            delay_cost_bps_mean = _safe_float(delay_mean)
            delay_cost_bps_p95 = _safe_float(delay_p95)
            exec_cost_bps_mean = _safe_float(exec_mean)
            exec_cost_bps_p95 = _safe_float(exec_p95)

            # market_impact_bps is 0.0 for single-lot trades (negligible impact).
            impact_bps_mean = 0.0

            total_cost_bps_mean = commission_bps + tax_bps + delay_cost_bps_mean + exec_cost_bps_mean + impact_bps_mean
            total_cost_bps_p95 = delay_cost_bps_p95 + exec_cost_bps_p95

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
                    delay_cost_bps_mean=delay_cost_bps_mean,
                    delay_cost_bps_p95=delay_cost_bps_p95,
                    exec_cost_bps_mean=exec_cost_bps_mean,
                    exec_cost_bps_p95=exec_cost_bps_p95,
                    impact_bps_mean=impact_bps_mean,
                    total_cost_bps_mean=total_cost_bps_mean,
                    total_cost_bps_p95=total_cost_bps_p95,
                )
            )
        return reports
