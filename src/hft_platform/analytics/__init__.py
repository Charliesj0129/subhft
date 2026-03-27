"""Shared ClickHouse aggregation queries for TCA, PnL, and feasibility reports."""

from hft_platform.analytics.queries import (
    query_daily_pnl,
    query_fill_quality,
    query_liquidity_gate_stats,
    query_slippage_distribution,
)

__all__ = [
    "query_daily_pnl",
    "query_fill_quality",
    "query_liquidity_gate_stats",
    "query_slippage_distribution",
]
