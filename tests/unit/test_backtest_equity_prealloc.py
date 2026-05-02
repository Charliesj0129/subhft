"""WU-03: Tests for pre-allocated equity buffers in HftBacktestAdapter."""

from __future__ import annotations

import numpy as np

from hft_platform.backtest import adapter as hbt_adapter
from hft_platform.strategy.base import BaseStrategy


# ---------------------------------------------------------------------------
# Shared mocks
# ---------------------------------------------------------------------------
class _Depth:
    best_bid = 10000
    best_ask = 10010


class _Hbt:
    def __init__(self, *a, **kw):
        self._ran = False
        self.current_timestamp = 123
        self.submitted = []

    def wait_next_feed(self, *_a, **_kw):
        if self._ran:
            return 1
        self._ran = True
        return 2

    def depth(self, *_a, **_kw):
        return _Depth()

    def position(self, *_a, **_kw):
        return 0

    def submit_buy_order(self, *a, **kw):
        self.submitted.append(a)

    def submit_sell_order(self, *a, **kw):
        self.submitted.append(a)

    def cancel(self, *_a, **_kw):
        pass

    def close(self):
        return True


class _BacktestAsset:
    def data(self, *_a, **_kw):
        return self

    def linear_asset(self, *_a, **_kw):
        return self

    def power_prob_queue_model(self, *_a, **_kw):
        return self

    def int_order_id_converter(self):
        return self


class _NoopStrategy(BaseStrategy):
    def on_stats(self, event):
        pass


def _patch(monkeypatch):
    monkeypatch.setattr(hbt_adapter, "HFTBACKTEST_AVAILABLE", True, raising=False)
    monkeypatch.setattr(hbt_adapter, "HashMapMarketDepthBacktest", _Hbt, raising=False)
    monkeypatch.setattr(hbt_adapter, "BacktestAsset", _BacktestAsset, raising=False)
    monkeypatch.setattr(hbt_adapter, "IOC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "GTC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "LIMIT", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "_detect_wait_status_mode", lambda: "modern", raising=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_equity_buffers_are_numpy(monkeypatch):
    """Equity storage uses pre-allocated numpy arrays, not list."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data="d",
    )
    assert isinstance(adapter._equity_ts_buf, np.ndarray)
    assert isinstance(adapter._equity_val_buf, np.ndarray)
    assert adapter._equity_ts_buf.dtype == np.int64
    assert adapter._equity_val_buf.dtype == np.float64


def test_equity_no_list_append(monkeypatch):
    """No list.append — count increments via index write."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data="d",
        equity_sample_ns=1,
    )
    adapter._reset_equity_buffers()
    adapter._maybe_record_equity_point(100, 10000, 10010)
    assert adapter._equity_count == 1
    assert adapter._equity_ts_buf[0] == 100


def test_equity_timestamps_monotonic(monkeypatch):
    """Recorded equity timestamps are monotonically increasing."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data="d",
        equity_sample_ns=1,
    )
    adapter._reset_equity_buffers()
    for ts in [100, 200, 300, 400]:
        adapter._maybe_record_equity_point(ts, 10000, 10010)

    ts_arr = adapter.equity_timestamps_ns
    assert len(ts_arr) == 4
    assert np.all(np.diff(ts_arr) > 0)


def test_equity_slice_properties_zero_copy(monkeypatch):
    """equity_timestamps_ns and equity_values return numpy slices (no copy)."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data="d",
        equity_sample_ns=1,
    )
    adapter._reset_equity_buffers()
    adapter._maybe_record_equity_point(100, 10000, 10010)
    adapter._maybe_record_equity_point(200, 10000, 10010)

    ts = adapter.equity_timestamps_ns
    vals = adapter.equity_values
    assert isinstance(ts, np.ndarray)
    assert isinstance(vals, np.ndarray)
    assert len(ts) == 2
    assert len(vals) == 2
    # Verify it's a view/slice (shares memory with buffer)
    assert np.shares_memory(ts, adapter._equity_ts_buf)


def test_equity_capacity_overflow_handled(monkeypatch):
    """Buffer doubles when capacity exceeded — no crash."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data="d",
        equity_sample_ns=1,
    )
    # Force tiny capacity
    adapter._equity_ts_buf = np.zeros(2, dtype=np.int64)
    adapter._equity_val_buf = np.zeros(2, dtype=np.float64)
    adapter._equity_count = 0

    for i in range(5):
        adapter._maybe_record_equity_point(i * 10, 10000, 10010)

    assert adapter._equity_count == 5
    assert adapter._equity_ts_buf.size >= 5


def test_equity_reset_clears_count(monkeypatch):
    """_reset_equity_buffers resets count without reallocating."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data="d",
        equity_sample_ns=1,
    )
    adapter._maybe_record_equity_point(100, 10000, 10010)
    assert adapter._equity_count >= 1
    adapter._reset_equity_buffers()
    assert adapter._equity_count == 0
