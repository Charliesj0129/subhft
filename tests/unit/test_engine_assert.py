"""Tests for MonitorEngine._bootstrap_symbol runtime error handling."""

from __future__ import annotations

import pytest

from hft_platform.monitor._engine import MonitorEngine
from hft_platform.monitor._types import MonitorConfig, SymbolState, WatchlistSymbol


def test_bootstrap_symbol_raises_if_no_data_source() -> None:
    """Test that _bootstrap_symbol raises RuntimeError when data source is not initialized."""
    config = MonitorConfig(
        symbols=(
            WatchlistSymbol(
                code="2330",
                name="TSMC",
                product_type="stock",
            ),
        ),
    )
    engine = MonitorEngine(config)

    # Create a minimal SymbolState
    ss = SymbolState(
        symbol=WatchlistSymbol(
            code="2330",
            name="TSMC",
            product_type="stock",
        ),
    )

    # Verify _data_source is None
    assert engine._data_source is None

    # Should raise RuntimeError, not AssertionError
    with pytest.raises(RuntimeError, match="_bootstrap_symbol called before data source initialized"):
        engine._bootstrap_symbol(ss)
