"""ClickHouse aggregation queries for analytics and feasibility reporting."""
from __future__ import annotations

from typing import Any


def query_daily_pnl(ch_client: Any, date_str: str) -> list[dict[str, Any]]:
    rows = ch_client.execute(
        """
        SELECT
            strategy_id,
            symbol,
            count(*) AS fill_count,
            sum(qty) AS total_qty,
            sum(fee_scaled + tax_scaled) / 10000 AS total_cost_ntd
        FROM hft.fills
        WHERE toDate(ts_exchange / 1000000000) = %(date)s
        GROUP BY strategy_id, symbol
        ORDER BY strategy_id, symbol
        """,
        {"date": date_str},
    )
    return [
        {
            "strategy": r[0], "symbol": r[1], "fill_count": r[2],
            "total_qty": r[3], "total_cost_ntd": r[4],
        }
        for r in rows
    ]


def query_slippage_distribution(ch_client: Any, date_str: str) -> list[dict[str, Any]]:
    rows = ch_client.execute(
        """
        SELECT
            symbol,
            count(*) AS n,
            avg(slippage_ticks) AS avg_ticks,
            quantile(0.95)(slippage_ticks) AS p95_ticks
        FROM hft.slippage_records
        WHERE toDate(ts / 1000000000) = %(date)s
        GROUP BY symbol
        """,
        {"date": date_str},
    )
    return [
        {"symbol": r[0], "count": r[1], "avg_ticks": r[2], "p95_ticks": r[3]}
        for r in rows
    ]


def query_fill_quality(ch_client: Any, date_str: str) -> list[dict[str, Any]]:
    rows = ch_client.execute(
        """
        SELECT
            strategy_id,
            symbol,
            count(*) AS n,
            avg(latency_ns) / 1e6 AS avg_latency_ms,
            quantile(0.95)(latency_ns) / 1e6 AS p95_latency_ms
        FROM hft.slippage_records
        WHERE toDate(ts / 1000000000) = %(date)s
        GROUP BY strategy_id, symbol
        """,
        {"date": date_str},
    )
    return [
        {
            "strategy": r[0], "symbol": r[1], "count": r[2],
            "avg_latency_ms": r[3], "p95_latency_ms": r[4],
        }
        for r in rows
    ]


def query_liquidity_gate_stats(ch_client: Any, date_str: str) -> list[dict[str, Any]]:
    rows = ch_client.execute(
        """
        SELECT
            symbol,
            countIf(result = 'rejected') AS rejected,
            countIf(result = 'passed') AS passed,
            count(*) AS total
        FROM hft.liquidity_gate_events
        WHERE toDate(ts / 1000000000) = %(date)s
        GROUP BY symbol
        """,
        {"date": date_str},
    )
    return [
        {"symbol": r[0], "rejected": r[1], "passed": r[2], "total": r[3]}
        for r in rows
    ]
