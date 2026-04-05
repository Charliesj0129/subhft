"""Tests for price rescaling in backtest adapter.

hftbacktest works with float prices (descaled).  convert.py divides platform
scaled-int prices by price_scale (10000) before writing NPZ data.  The adapter
must therefore multiply hftbacktest float prices back by price_scale when
constructing BidAskEvent, LOBStatsEvent, equity, and fill records.
"""

from __future__ import annotations

import numpy as np

from hft_platform.backtest import adapter as hbt_adapter
from hft_platform.strategy.base import BaseStrategy

# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

class _Depth:
    """Minimal depth mock returning float prices like real hftbacktest."""

    def __init__(self, best_bid: float, best_ask: float, bid_qty: int = 5, ask_qty: int = 3):
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.best_bid_qty = bid_qty
        self.best_ask_qty = ask_qty


class _Hbt:
    def __init__(self, depth_obj: _Depth | None = None):
        self._ran = False
        self.current_timestamp = 100_000_000
        self._depth = depth_obj or _Depth(1.0, 1.001)
        self.submitted: list = []

    def wait_next_feed(self, *_a, **_kw) -> int:
        if self._ran:
            return 1  # end-of-data
        self._ran = True
        return 2  # feed event

    def depth(self, *_a, **_kw) -> _Depth:
        return self._depth

    def position(self, *_a, **_kw) -> int:
        return 0

    def submit_buy_order(self, *a, **kw) -> None:
        self.submitted.append(a)

    def submit_sell_order(self, *a, **kw) -> None:
        self.submitted.append(a)

    def cancel(self, *_a, **_kw) -> None:
        pass

    def close(self) -> bool:
        return True


class _BacktestAsset:
    def data(self, *_a, **_kw) -> "_BacktestAsset":
        return self

    def linear_asset(self, *_a, **_kw) -> "_BacktestAsset":
        return self

    def power_prob_queue_model(self, *_a, **_kw) -> "_BacktestAsset":
        return self

    def int_order_id_converter(self) -> "_BacktestAsset":
        return self


class _NoopStrategy(BaseStrategy):
    def on_stats(self, event) -> None:  # type: ignore[override]
        pass


def _patch(monkeypatch, depth: _Depth | None = None) -> None:
    hbt_instance = _Hbt(depth_obj=depth)
    monkeypatch.setattr(hbt_adapter, "HFTBACKTEST_AVAILABLE", True, raising=False)
    monkeypatch.setattr(hbt_adapter, "HashMapMarketDepthBacktest", lambda *a, **kw: hbt_instance, raising=False)
    monkeypatch.setattr(hbt_adapter, "BacktestAsset", _BacktestAsset, raising=False)
    monkeypatch.setattr(hbt_adapter, "IOC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "GTC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "LIMIT", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "_detect_wait_status_mode", lambda: "modern", raising=False)


# ---------------------------------------------------------------------------
# _build_l1_bidask_event rescaling
# ---------------------------------------------------------------------------

def test_build_l1_bidask_event_rescales_to_x10000(monkeypatch):
    """_build_l1_bidask_event must store prices as scaled ints (x10000)."""
    depth = _Depth(best_bid=100.5, best_ask=100.6, bid_qty=10, ask_qty=8)
    _patch(monkeypatch, depth=depth)

    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
        price_scale=10_000,
    )
    event = adapter._build_l1_bidask_event(depth, ts_ns=1_000_000)

    assert event.bids[0, 0] == 1_005_000   # 100.5 * 10000
    assert event.asks[0, 0] == 1_006_000   # 100.6 * 10000


def test_build_l1_bidask_event_zero_price(monkeypatch):
    """Zero float price maps to zero scaled int without error."""
    depth = _Depth(best_bid=0.0, best_ask=0.0)
    _patch(monkeypatch, depth=depth)

    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    event = adapter._build_l1_bidask_event(depth, ts_ns=0)

    assert event.bids[0, 0] == 0
    assert event.asks[0, 0] == 0


def test_build_l1_bidask_event_tick_level_prices(monkeypatch):
    """Adjacent tick prices separated by 0.5 NTD round correctly after rescaling."""
    # Smallest futures tick for TMFD6 is 0.5 points ≈ 5000 in x10000
    depth = _Depth(best_bid=10000.0, best_ask=10000.5, bid_qty=1, ask_qty=1)
    _patch(monkeypatch, depth=depth)

    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
        price_scale=10_000,
    )
    event = adapter._build_l1_bidask_event(depth, ts_ns=0)

    # 10000.0 * 10000 = 100_000_000; 10000.5 * 10000 = 100_005_000
    assert event.bids[0, 0] == 100_000_000
    assert event.asks[0, 0] == 100_005_000
    # Spread must be exactly one tick (5000 units)
    assert event.asks[0, 0] - event.bids[0, 0] == 5_000


def test_build_l1_bidask_event_qty_unaffected(monkeypatch):
    """Quantities in BidAskEvent are not scaled — they remain raw."""
    depth = _Depth(best_bid=100.0, best_ask=100.1, bid_qty=7, ask_qty=4)
    _patch(monkeypatch, depth=depth)

    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    event = adapter._build_l1_bidask_event(depth, ts_ns=0)

    assert event.bids[0, 1] == 7
    assert event.asks[0, 1] == 4


# ---------------------------------------------------------------------------
# get_mid_price_x2 rescaling
# ---------------------------------------------------------------------------

def test_get_mid_price_x2_rescales(monkeypatch):
    """get_mid_price_x2 returns sum of both prices scaled by x10000."""
    depth = _Depth(best_bid=1.0, best_ask=1.001)
    _patch(monkeypatch, depth=depth)

    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
        price_scale=10_000,
    )
    # 1.0 * 10000 = 10000; 1.001 * 10000 = 10010 → sum = 20010
    assert adapter.get_mid_price_x2() == 20_010


def test_get_mid_price_x2_is_int(monkeypatch):
    """get_mid_price_x2 returns a Python int, not a float."""
    depth = _Depth(best_bid=1.5, best_ask=1.502)
    _patch(monkeypatch, depth=depth)

    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    result = adapter.get_mid_price_x2()
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# Rounding — no truncation on fractional float prices
# ---------------------------------------------------------------------------

def test_no_truncation_on_fractional_price(monkeypatch):
    """int(round(...)) must not truncate fractional float prices.

    Before the fix: int(100.9999) == 100 (truncation).
    After the fix:  int(round(0.01009999 * 10000)) == 101 (rounded correctly).
    """
    depth = _Depth(best_bid=0.01009999, best_ask=0.0101)
    _patch(monkeypatch, depth=depth)

    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
        price_scale=10_000,
    )
    event = adapter._build_l1_bidask_event(depth, ts_ns=0)

    # Without round(): int(0.01009999 * 10000) = int(100.9999) = 100 (WRONG)
    # With round():    int(round(0.01009999 * 10000)) = int(101.0) = 101 (CORRECT)
    assert event.bids[0, 0] == 101
    assert event.asks[0, 0] == 101


# ---------------------------------------------------------------------------
# Dtype integrity
# ---------------------------------------------------------------------------

def test_build_l1_bidask_event_arrays_are_int64(monkeypatch):
    """BidAskEvent arrays must be int64 per platform convention."""
    depth = _Depth(best_bid=100.0, best_ask=100.5)
    _patch(monkeypatch, depth=depth)

    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    event = adapter._build_l1_bidask_event(depth, ts_ns=0)

    assert event.bids.dtype == np.int64
    assert event.asks.dtype == np.int64
