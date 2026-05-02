"""Tests for WAL loader table name mapping (P-05).

Verifies that fill WAL files are correctly mapped to hft.fills (not hft.trades),
and that legacy trades filenames still map to hft.trades.
"""

import pytest

from hft_platform.recorder._loader_wal import parse_batch_table_name, parse_table_from_filename


def test_parse_table_from_filename_fills_maps_to_fills():
    """fills_ filenames must replay into hft.fills, not hft.trades."""
    result = parse_table_from_filename("fills_1234567890.jsonl")
    assert result == "fills", f"Expected 'fills', got {result!r}"


def test_parse_table_from_filename_fills_with_hft_prefix_maps_to_fills():
    """hft.fills_ prefixed filenames must also map to fills."""
    result = parse_table_from_filename("hft.fills_1234567890.jsonl")
    assert result == "fills", f"Expected 'fills', got {result!r}"


def test_parse_table_from_filename_trades_maps_to_trades_legacy():
    """Legacy trades_ filenames must still map to hft.trades."""
    result = parse_table_from_filename("trades_1234567890.jsonl")
    assert result == "trades", f"Expected 'trades', got {result!r}"


def test_parse_table_from_filename_market_data_unchanged():
    """market_data filenames should continue to map to market_data."""
    result = parse_table_from_filename("market_data_1234567890.jsonl")
    assert result == "market_data", f"Expected 'market_data', got {result!r}"


def test_parse_table_from_filename_orders_unchanged():
    """orders filenames should continue to map to orders."""
    result = parse_table_from_filename("orders_1234567890.jsonl")
    assert result == "orders", f"Expected 'orders', got {result!r}"


def test_parse_batch_table_name_fills_maps_to_fills():
    """Batch __wal_table__ header 'fills' must map to fills."""
    result = parse_batch_table_name("fills")
    assert result == "fills", f"Expected 'fills', got {result!r}"


def test_parse_batch_table_name_hft_fills_maps_to_fills():
    """Batch __wal_table__ header 'hft.fills' must map to fills."""
    result = parse_batch_table_name("hft.fills")
    assert result == "fills", f"Expected 'fills', got {result!r}"


def test_parse_batch_table_name_trades_maps_to_trades_legacy():
    """Batch __wal_table__ header 'trades' must still map to trades for legacy data."""
    result = parse_batch_table_name("trades")
    assert result == "trades", f"Expected 'trades', got {result!r}"


def test_parse_batch_table_name_market_data_unchanged():
    """Batch __wal_table__ header 'market_data' must remain market_data."""
    result = parse_batch_table_name("market_data")
    assert result == "market_data", f"Expected 'market_data', got {result!r}"


def test_parse_batch_table_name_unknown_raises():
    """Unknown batch table names must raise ValueError."""
    with pytest.raises(ValueError, match="Unknown table name"):
        parse_batch_table_name("nonexistent_table")
