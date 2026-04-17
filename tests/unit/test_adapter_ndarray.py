"""Test HftBacktestAdapter accepts ndarray input (from ChDataSource)."""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("hftbacktest")

from hft_platform.backtest.adapter import HFTBACKTEST_AVAILABLE, HftBacktestAdapter
from hft_platform.backtest.ch_data_source import (
    BUY_EVENT,
    DEPTH_EVENT,
    EXCH_EVENT,
    SELL_EVENT,
    TRADE_EVENT,
    _event_dtype,
)


def _minimal_events() -> np.ndarray:
    """Minimal valid event array for adapter construction."""
    dtype = _event_dtype()
    return np.array(
        [
            (DEPTH_EVENT | EXCH_EVENT | BUY_EVENT,
             1_000_000_000, 1_001_000_000, 17000.0, 5, 0, 0, 0.0),
            (DEPTH_EVENT | EXCH_EVENT | SELL_EVENT,
             1_000_000_000, 1_001_000_000, 17001.0, 3, 0, 0, 0.0),
            (TRADE_EVENT | EXCH_EVENT | BUY_EVENT,
             2_000_000_000, 2_001_000_000, 17000.5, 1, 0, 0, 0.0),
        ],
        dtype=dtype,
    )


def _make_null_strategy():
    from hft_platform.strategy.base import BaseStrategy

    class NullStrategy(BaseStrategy):
        def handle_event(self, event):
            return []

        def on_start(self):
            pass

        def on_stop(self):
            pass

    return NullStrategy("null")


def test_adapter_accepts_ndarray_data():
    """Adapter construction succeeds when data is a numpy ndarray."""
    if not HFTBACKTEST_AVAILABLE:
        pytest.skip("hftbacktest not installed")

    events = _minimal_events()
    adapter = HftBacktestAdapter(
        strategy=_make_null_strategy(),
        asset_symbol="TMFD6",
        data=events,  # ndarray input
        tick_size=1.0,
        lot_size=1.0,
    )
    # Verify the adapter accepted the ndarray
    assert adapter._data_ndarray is events


def test_adapter_str_path_backward_compat(tmp_path):
    """Adapter construction with str data_path still works (backward compat)."""
    if not HFTBACKTEST_AVAILABLE:
        pytest.skip("hftbacktest not installed")

    # Build a valid .npz file with minimal event data
    dtype = _event_dtype()
    events = np.array(
        [
            (DEPTH_EVENT | EXCH_EVENT | BUY_EVENT,
             1_000_000_000, 1_001_000_000, 17000.0, 5, 0, 0, 0.0),
            (DEPTH_EVENT | EXCH_EVENT | SELL_EVENT,
             1_000_000_000, 1_001_000_000, 17001.0, 3, 0, 0, 0.0),
            (TRADE_EVENT | EXCH_EVENT | BUY_EVENT,
             2_000_000_000, 2_001_000_000, 17000.5, 1, 0, 0, 0.0),
        ],
        dtype=dtype,
    )
    npz_path = tmp_path / "test_data.npz"
    np.savez(str(npz_path), data=events)

    adapter = HftBacktestAdapter(
        strategy=_make_null_strategy(),
        asset_symbol="TMFD6",
        data=str(npz_path),
        tick_size=1.0,
        lot_size=1.0,
    )
    # Verify str path is stored correctly
    assert adapter.data_path == str(npz_path)
    assert adapter._data_ndarray is None


def test_adapter_ndarray_infers_tick_size():
    """When data_path is ndarray and tick_size=None, tick size is inferred from the array."""
    if not HFTBACKTEST_AVAILABLE:
        pytest.skip("hftbacktest not installed")

    events = _minimal_events()
    # Should not raise even without explicit tick_size
    adapter = HftBacktestAdapter(
        strategy=_make_null_strategy(),
        asset_symbol="TMFD6",
        data=events,
        # tick_size omitted to trigger inference path
        lot_size=1.0,
    )
    assert adapter._data_ndarray is events
