"""WU-02: Tests for SoA fill log in HftBacktestAdapter."""

from __future__ import annotations

import numpy as np
import pytest

from hft_platform.backtest import adapter as hbt_adapter
from hft_platform.strategy.base import BaseStrategy


# ---------------------------------------------------------------------------
# Test fixtures (shared mock infrastructure)
# ---------------------------------------------------------------------------
class _Depth:
    best_bid = 10000
    best_ask = 10010


class _Hbt:
    def __init__(self, *a, **kw):
        self._calls = 0
        self._max_calls = 3
        self.current_timestamp = 1_000_000
        self.submitted = []
        self._position = 0

    def wait_next_feed(self, *_a, **_kw):
        if self._calls >= self._max_calls:
            return 1  # end
        self._calls += 1
        self.current_timestamp += 1_000_000
        return 2  # feed available (modern)

    def depth(self, *_a, **_kw):
        return _Depth()

    def position(self, *_a, **_kw):
        return self._position

    def submit_buy_order(self, *a, **kw):
        self.submitted.append(("buy", *a))
        self._position += a[3] if len(a) > 3 else 1

    def submit_sell_order(self, *a, **kw):
        self.submitted.append(("sell", *a))

    def cancel(self, *_a, **_kw):
        pass

    def close(self):
        return True


class _BacktestAsset:
    def data(self, *_a, **_kw):
        return self

    def linear_asset(self, *_a, **_kw):
        return self

    def constant_latency(self, *_a, **_kw):
        return self

    def power_prob_queue_model(self, *_a, **_kw):
        return self

    def int_order_id_converter(self):
        return self


class _BuyStrategy(BaseStrategy):
    """Strategy that buys on every tick — triggers fills for testing."""

    def on_stats(self, event):
        self.buy(event.symbol, event.best_bid, 1)


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
def test_fill_log_is_numpy_soa(monkeypatch):
    """Fill storage uses pre-allocated numpy arrays, not list[dict]."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_BuyStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    assert isinstance(adapter._fill_ts_ns, np.ndarray)
    assert isinstance(adapter._fill_delta, np.ndarray)
    assert isinstance(adapter._fill_position_after, np.ndarray)
    assert isinstance(adapter._fill_mid_price_x2, np.ndarray)
    assert adapter._fill_ts_ns.dtype == np.int64
    assert adapter._fill_delta.dtype == np.int32
    assert adapter._fill_mid_price_x2.dtype == np.int64


def test_fill_log_no_list_append(monkeypatch):
    """No list.append on hot path — fill count increments via index."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_BuyStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    assert adapter._fill_count == 0
    adapter._record_fill(100, 1, 1, 20010)
    assert adapter._fill_count == 1
    assert adapter._fill_ts_ns[0] == 100
    assert adapter._fill_delta[0] == 1
    assert adapter._fill_mid_price_x2[0] == 20010


def test_fill_log_capacity_overflow_handled(monkeypatch):
    """Buffer doubles when capacity is exceeded — no crash."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_BuyStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    # Force small capacity for test
    adapter._fill_ts_ns = np.zeros(2, dtype=np.int64)
    adapter._fill_delta = np.zeros(2, dtype=np.int32)
    adapter._fill_position_after = np.zeros(2, dtype=np.int32)
    adapter._fill_mid_price_x2 = np.zeros(2, dtype=np.int64)
    adapter._fill_count = 0

    for i in range(5):
        adapter._record_fill(i * 100, 1, i + 1, 20000 + i)

    assert adapter._fill_count == 5
    assert adapter._fill_ts_ns.size >= 5
    assert adapter._fill_ts_ns[4] == 400
    assert adapter._fill_mid_price_x2[4] == 20004


def test_fill_stats_vectorized(monkeypatch):
    """fill_stats computes from SoA arrays, not list iteration."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_BuyStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    adapter._total_buy_fills = 3
    adapter._total_sell_fills = 1
    # 4 fills
    for i in range(4):
        delta = 1 if i < 3 else -1
        adapter._record_fill(i * 1_000_000_000, delta, i + 1, 20000 + i * 10)

    stats = adapter.fill_stats
    assert stats["buy_fills"] == 3
    assert stats["sell_fills"] == 1
    assert stats["total_fills"] == 4
    assert stats["n_fill_events"] == 4
    assert stats["fill_rate_per_hour"] > 0


def test_fill_stats_adverse_selection(monkeypatch):
    """Adverse selection computed from int mid_price_x2 differences."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_BuyStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    adapter._total_buy_fills = 2
    # Buy at mid_x2=20000, next mid_x2=19990 (price dropped → adverse for buyer)
    adapter._record_fill(0, 1, 1, 20000)
    adapter._record_fill(1_000_000, 1, 2, 19990)

    stats = adapter.fill_stats
    # adverse for buy = -(19990-20000)/2 = 5.0
    assert stats["adverse_selection_mean"] == pytest.approx(5.0)


def test_fill_log_backward_compat(monkeypatch):
    """_fill_log property returns list[dict] for backward compat."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_BuyStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    adapter._record_fill(100, 1, 1, 20010)
    log = adapter._fill_log
    assert isinstance(log, list)
    assert isinstance(log[0], dict)
    assert log[0]["ts_ns"] == 100
    assert log[0]["delta"] == 1
    assert log[0]["mid_price"] == pytest.approx(10005.0)
    assert log[0]["mid_price_x2"] == 20010


def test_fill_mid_price_x2_is_integer(monkeypatch):
    """mid_price_x2 stored as int (Precision Law)."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_BuyStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    adapter._record_fill(0, 1, 1, 20010)
    assert adapter._fill_mid_price_x2.dtype == np.int64
    assert isinstance(int(adapter._fill_mid_price_x2[0]), int)
    assert adapter._fill_mid_price_x2[0] == 20010


def test_fill_count_starts_at_zero(monkeypatch):
    """Fresh adapter has zero fills."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_BuyStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    assert adapter._fill_count == 0
    assert adapter.fill_stats["n_fill_events"] == 0
