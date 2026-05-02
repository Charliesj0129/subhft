"""Unit tests for hft_platform.analytics.queries."""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.analytics.queries import (
    query_daily_pnl,
    query_fill_quality,
    query_liquidity_gate_stats,
    query_slippage_distribution,
)

DATE = "2026-03-31"


# ---------------------------------------------------------------------------
# query_daily_pnl
# ---------------------------------------------------------------------------


def test_query_daily_pnl_returns_structured_data():
    client = MagicMock()
    client.execute.return_value = [("strat1", "TXFD6", 10, 50, 125.5)]
    result = query_daily_pnl(client, DATE)
    assert len(result) == 1
    row = result[0]
    assert row["strategy"] == "strat1"
    assert row["symbol"] == "TXFD6"
    assert row["fill_count"] == 10
    assert row["total_qty"] == 50
    assert row["total_cost_ntd"] == 125.5


def test_query_daily_pnl_empty_returns_empty_list():
    client = MagicMock()
    client.execute.return_value = []
    result = query_daily_pnl(client, DATE)
    assert result == []


def test_query_daily_pnl_passes_date_as_parameter():
    client = MagicMock()
    client.execute.return_value = []
    query_daily_pnl(client, DATE)
    call_args = client.execute.call_args
    sql, params = call_args[0]
    assert "%(date)s" in sql
    assert params == {"date": DATE}


def test_query_daily_pnl_multiple_rows():
    client = MagicMock()
    client.execute.return_value = [
        ("strat1", "TXFD6", 5, 20, 60.0),
        ("strat2", "2330", 3, 15, 45.0),
    ]
    result = query_daily_pnl(client, DATE)
    assert len(result) == 2
    assert result[0]["strategy"] == "strat1"
    assert result[1]["strategy"] == "strat2"
    assert result[1]["symbol"] == "2330"


def test_query_daily_pnl_contains_fills_table_reference():
    client = MagicMock()
    client.execute.return_value = []
    query_daily_pnl(client, DATE)
    sql = client.execute.call_args[0][0]
    assert "hft.fills" in sql


# ---------------------------------------------------------------------------
# query_slippage_distribution
# ---------------------------------------------------------------------------


def test_query_slippage_distribution_returns_structured_data():
    client = MagicMock()
    client.execute.return_value = [("TXFD6", 100, 0.5, 1.2)]
    result = query_slippage_distribution(client, DATE)
    assert len(result) == 1
    row = result[0]
    assert row["symbol"] == "TXFD6"
    assert row["count"] == 100
    assert row["avg_ticks"] == 0.5
    assert row["p95_ticks"] == 1.2


def test_query_slippage_distribution_empty_returns_empty_list():
    client = MagicMock()
    client.execute.return_value = []
    result = query_slippage_distribution(client, DATE)
    assert result == []


def test_query_slippage_distribution_passes_date_as_parameter():
    client = MagicMock()
    client.execute.return_value = []
    query_slippage_distribution(client, DATE)
    call_args = client.execute.call_args
    sql, params = call_args[0]
    assert "%(date)s" in sql
    assert params == {"date": DATE}


def test_query_slippage_distribution_multiple_rows():
    client = MagicMock()
    client.execute.return_value = [
        ("TXFD6", 80, 0.3, 0.9),
        ("2330", 40, 0.8, 2.0),
    ]
    result = query_slippage_distribution(client, DATE)
    assert len(result) == 2
    assert result[0]["symbol"] == "TXFD6"
    assert result[1]["symbol"] == "2330"
    assert result[1]["p95_ticks"] == 2.0


# ---------------------------------------------------------------------------
# query_fill_quality
# ---------------------------------------------------------------------------


def test_query_fill_quality_returns_structured_data():
    client = MagicMock()
    client.execute.return_value = [("strat1", "TXFD6", 50, 2.5, 8.0)]
    result = query_fill_quality(client, DATE)
    assert len(result) == 1
    row = result[0]
    assert row["strategy"] == "strat1"
    assert row["symbol"] == "TXFD6"
    assert row["count"] == 50
    assert row["avg_latency_ms"] == 2.5
    assert row["p95_latency_ms"] == 8.0


def test_query_fill_quality_empty_returns_empty_list():
    client = MagicMock()
    client.execute.return_value = []
    result = query_fill_quality(client, DATE)
    assert result == []


def test_query_fill_quality_passes_date_as_parameter():
    client = MagicMock()
    client.execute.return_value = []
    query_fill_quality(client, DATE)
    call_args = client.execute.call_args
    sql, params = call_args[0]
    assert "%(date)s" in sql
    assert params == {"date": DATE}


def test_query_fill_quality_multiple_rows():
    client = MagicMock()
    client.execute.return_value = [
        ("strat1", "TXFD6", 30, 1.5, 5.0),
        ("strat2", "2330", 20, 3.0, 12.0),
    ]
    result = query_fill_quality(client, DATE)
    assert len(result) == 2
    assert result[0]["strategy"] == "strat1"
    assert result[1]["p95_latency_ms"] == 12.0


def test_query_fill_quality_contains_slippage_records_table():
    client = MagicMock()
    client.execute.return_value = []
    query_fill_quality(client, DATE)
    sql = client.execute.call_args[0][0]
    assert "hft.slippage_records" in sql


# ---------------------------------------------------------------------------
# query_liquidity_gate_stats
# ---------------------------------------------------------------------------


def test_query_liquidity_gate_stats_returns_structured_data():
    client = MagicMock()
    client.execute.return_value = [("TXFD6", 15, 85, 100)]
    result = query_liquidity_gate_stats(client, DATE)
    assert len(result) == 1
    row = result[0]
    assert row["symbol"] == "TXFD6"
    assert row["rejected"] == 15
    assert row["passed"] == 85
    assert row["total"] == 100


def test_query_liquidity_gate_stats_empty_returns_empty_list():
    client = MagicMock()
    client.execute.return_value = []
    result = query_liquidity_gate_stats(client, DATE)
    assert result == []


def test_query_liquidity_gate_stats_passes_date_as_parameter():
    client = MagicMock()
    client.execute.return_value = []
    query_liquidity_gate_stats(client, DATE)
    call_args = client.execute.call_args
    sql, params = call_args[0]
    assert "%(date)s" in sql
    assert params == {"date": DATE}


def test_query_liquidity_gate_stats_multiple_rows():
    client = MagicMock()
    client.execute.return_value = [
        ("TXFD6", 10, 90, 100),
        ("2330", 5, 45, 50),
    ]
    result = query_liquidity_gate_stats(client, DATE)
    assert len(result) == 2
    assert result[0]["symbol"] == "TXFD6"
    assert result[1]["symbol"] == "2330"
    assert result[1]["total"] == 50


def test_query_liquidity_gate_stats_rejected_plus_passed_equals_total():
    client = MagicMock()
    client.execute.return_value = [("TXFD6", 7, 93, 100)]
    result = query_liquidity_gate_stats(client, DATE)
    row = result[0]
    assert row["rejected"] + row["passed"] == row["total"]


def test_query_liquidity_gate_stats_contains_liquidity_gate_events_table():
    client = MagicMock()
    client.execute.return_value = []
    query_liquidity_gate_stats(client, DATE)
    sql = client.execute.call_args[0][0]
    assert "hft.liquidity_gate_events" in sql
