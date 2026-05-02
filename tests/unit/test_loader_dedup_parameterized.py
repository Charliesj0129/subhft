"""Tests for S2 audit fix: WAL dedup query must not use f-string SQL."""

from __future__ import annotations

import ast
import inspect

import pytest


def test_is_duplicate_no_fstring_sql():
    """is_duplicate must not use f-string SQL interpolation."""
    from hft_platform.recorder import _loader_ch

    source = inspect.getsource(_loader_ch.is_duplicate)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    if "SELECT" in value.value.upper() or "FROM" in value.value.upper():
                        pytest.fail("is_duplicate() still uses f-string SQL interpolation")
    assert "SELECT" in source.upper()


def test_parse_batch_table_name_rejects_unknown():
    """parse_batch_table_name must reject unknown table names, not pass them through."""
    from hft_platform.recorder._loader_wal import parse_batch_table_name

    with pytest.raises(ValueError, match="Unknown table"):
        parse_batch_table_name("hft.exploit'; DROP TABLE; --")


def test_parse_batch_table_name_accepts_known_tables():
    """parse_batch_table_name must still accept all known table names."""
    from hft_platform.recorder._loader_wal import parse_batch_table_name

    assert parse_batch_table_name("market_data") == "market_data"
    assert parse_batch_table_name("hft.market_data") == "market_data"
    assert parse_batch_table_name("orders") == "orders"
    assert parse_batch_table_name("trades") == "trades"
    assert parse_batch_table_name("fills") == "fills"
    assert parse_batch_table_name("risk_log") == "risk_log"
    assert parse_batch_table_name("logs") == "risk_log"
    assert parse_batch_table_name("backtest_runs") == "backtest_runs"
    assert parse_batch_table_name("latency_spans") == "latency_spans"
