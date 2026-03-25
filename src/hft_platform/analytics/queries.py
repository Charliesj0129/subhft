"""Shared ClickHouse aggregation queries for daily reports, TCA, and scorecard."""
from __future__ import annotations

DAILY_FILLS_SUMMARY = """
SELECT
    toDate(ts / 1000000000) AS trade_date,
    strategy_id,
    symbol,
    count() AS fill_count,
    sum(CASE WHEN realized_pnl != 0 THEN 1 ELSE 0 END) AS pnl_fills,
    sum(realized_pnl) AS total_realized_pnl,
    sum(fee) AS total_fees,
    sum(tax) AS total_tax
FROM hft.trades
WHERE ts >= {start_ns:Int64} AND ts < {end_ns:Int64}
GROUP BY trade_date, strategy_id, symbol
ORDER BY trade_date
"""

DAILY_SLIPPAGE_SUMMARY = """
SELECT
    toDate(ts / 1000000000) AS trade_date,
    count() AS slip_count,
    avg(slippage_ticks) AS avg_slippage_ticks,
    sum(slippage_ntd) AS total_slippage_ntd,
    max(slippage_ticks) AS max_adverse_ticks
FROM hft.slippage_records
WHERE ts >= {start_ns:Int64} AND ts < {end_ns:Int64}
GROUP BY trade_date
ORDER BY trade_date
"""

DAILY_ORDERS_SUMMARY = """
SELECT
    toDate(ts / 1000000000) AS trade_date,
    count() AS total_orders,
    countIf(status = 'FILLED') AS filled,
    countIf(status = 'CANCELLED') AS cancelled
FROM hft.orders
WHERE ts >= {start_ns:Int64} AND ts < {end_ns:Int64}
GROUP BY trade_date
ORDER BY trade_date
"""

CUMULATIVE_REPORTS = """
SELECT
    report_date,
    net_pnl_ntd,
    win_count,
    loss_count,
    profit_factor,
    avg_slippage_ticks,
    soft_limit_triggers,
    hard_limit_triggers
FROM hft.daily_reports
WHERE strategy_id = {strategy_id:String}
ORDER BY report_date
"""

TCA_FILL_DETAIL = """
SELECT
    s.order_id,
    s.symbol,
    s.side,
    s.decision_mid,
    s.fill_price,
    s.slippage_ticks,
    s.slippage_ntd,
    s.latency_ns,
    s.ts,
    toHour(toDateTime(s.ts / 1000000000)) AS hour_of_day
FROM hft.slippage_records s
WHERE s.ts >= {start_ns:Int64} AND s.ts < {end_ns:Int64}
ORDER BY s.ts
"""
