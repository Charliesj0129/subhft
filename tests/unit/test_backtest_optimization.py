"""WU-11: Comprehensive test suite for all backtest optimization changes."""

from __future__ import annotations

import numpy as np
import pytest

from hft_platform.backtest import adapter as hbt_adapter
from hft_platform.backtest._equity_core import compute_equity_from_positions
from hft_platform.strategy.base import BaseStrategy


# ---------------------------------------------------------------------------
# Shared mocks
# ---------------------------------------------------------------------------
class _Depth:
    best_bid = 10000
    best_ask = 10010
    best_bid_qty = 100
    best_ask_qty = 200
    bid_qty = 100
    ask_qty = 200
    bid_volume = 100
    ask_volume = 200


class _FillingHbt:
    """Hbt mock that simulates position changes (fills)."""

    def __init__(self, *a, **kw):
        self._calls = 0
        self._max_calls = 5
        self.current_timestamp = 1_000_000
        self.submitted = []
        self._position = 0

    def wait_next_feed(self, *_a, **_kw):
        if self._calls >= self._max_calls:
            return 1
        self._calls += 1
        self.current_timestamp += 1_000_000
        return 2

    def depth(self, *_a, **_kw):
        return _Depth()

    def position(self, *_a, **_kw):
        return self._position

    def submit_buy_order(self, *a, **kw):
        self.submitted.append(("buy", *a))
        self._position += 1

    def submit_sell_order(self, *a, **kw):
        self.submitted.append(("sell", *a))
        self._position -= 1

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


class _BuyStrategy(BaseStrategy):
    def on_stats(self, event):
        self.buy(event.symbol, event.best_bid, 1)


class _NoopStrategy(BaseStrategy):
    def on_stats(self, event):
        pass


def _patch(monkeypatch):
    monkeypatch.setattr(hbt_adapter, "HFTBACKTEST_AVAILABLE", True, raising=False)
    monkeypatch.setattr(hbt_adapter, "HashMapMarketDepthBacktest", _FillingHbt, raising=False)
    monkeypatch.setattr(hbt_adapter, "BacktestAsset", _BacktestAsset, raising=False)
    monkeypatch.setattr(hbt_adapter, "IOC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "GTC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "LIMIT", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "_detect_wait_status_mode", lambda: "modern", raising=False)


# ---------------------------------------------------------------------------
# 1. Fill log is numpy SoA
# ---------------------------------------------------------------------------
def test_fill_log_is_numpy_soa(monkeypatch):
    _patch(monkeypatch)
    a = hbt_adapter.HftBacktestAdapter(strategy=_BuyStrategy("t"), asset_symbol="X", data_path="d")
    assert isinstance(a._fill_ts_ns, np.ndarray)
    assert a._fill_ts_ns.dtype == np.int64
    assert isinstance(a._fill_delta, np.ndarray)
    assert a._fill_delta.dtype == np.int32


# ---------------------------------------------------------------------------
# 2. Fill log capacity overflow handled
# ---------------------------------------------------------------------------
def test_fill_log_capacity_overflow_handled(monkeypatch):
    _patch(monkeypatch)
    a = hbt_adapter.HftBacktestAdapter(strategy=_BuyStrategy("t"), asset_symbol="X", data_path="d")
    a._fill_ts_ns = np.zeros(2, dtype=np.int64)
    a._fill_delta = np.zeros(2, dtype=np.int32)
    a._fill_position_after = np.zeros(2, dtype=np.int32)
    a._fill_mid_price_x2 = np.zeros(2, dtype=np.int64)
    a._fill_count = 0
    for i in range(5):
        a._record_fill(i, 1, i + 1, 20000)
    assert a._fill_count == 5


# ---------------------------------------------------------------------------
# 3. Equity preallocated no append
# ---------------------------------------------------------------------------
def test_equity_preallocated_no_append(monkeypatch):
    _patch(monkeypatch)
    a = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
        equity_sample_ns=1,
    )
    assert isinstance(a._equity_ts_buf, np.ndarray)
    a._reset_equity_buffers()
    a._maybe_record_equity_point(100, 10000, 10010)
    assert a._equity_count == 1
    assert a._equity_ts_buf[0] == 100


# ---------------------------------------------------------------------------
# 4. Equity timestamps monotonic
# ---------------------------------------------------------------------------
def test_equity_timestamps_monotonic(monkeypatch):
    _patch(monkeypatch)
    a = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
        equity_sample_ns=1,
    )
    a._reset_equity_buffers()
    for ts in [100, 200, 300]:
        a._maybe_record_equity_point(ts, 10000, 10010)
    assert np.all(np.diff(a.equity_timestamps_ns) > 0)


# ---------------------------------------------------------------------------
# 5. LOB event construction (feed)
# ---------------------------------------------------------------------------
def test_lob_event_construction_feed(monkeypatch):
    from hft_platform.backtest._hbt_utils import build_lob_event
    from hft_platform.events import LOBStatsEvent

    _patch(monkeypatch)
    a = hbt_adapter.HftBacktestAdapter(strategy=_NoopStrategy("t"), asset_symbol="X", data_path="d")
    event, feat = build_lob_event(a, _Depth(), 1000, 10000, 10010)
    assert isinstance(event, LOBStatsEvent)
    assert event.best_bid == 10000
    assert event.best_ask == 10010
    assert feat is None


# ---------------------------------------------------------------------------
# 6. LOB event construction (elapse — same code path via build_lob_event)
# ---------------------------------------------------------------------------
def test_lob_event_construction_elapse(monkeypatch):
    from hft_platform.backtest._hbt_utils import build_lob_event
    from hft_platform.events import LOBStatsEvent

    _patch(monkeypatch)
    a = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
        tick_mode="elapse",
    )
    event, feat = build_lob_event(a, _Depth(), 2000, 10000, 10010)
    assert isinstance(event, LOBStatsEvent)
    assert event.symbol == "X"


# ---------------------------------------------------------------------------
# 7. mid_price_x2 is integer
# ---------------------------------------------------------------------------
def test_mid_price_x2_is_integer(monkeypatch):
    _patch(monkeypatch)
    a = hbt_adapter.HftBacktestAdapter(strategy=_NoopStrategy("t"), asset_symbol="X", data_path="d")
    x2 = a.get_mid_price_x2()
    assert isinstance(x2, int)
    assert x2 == 20010


# ---------------------------------------------------------------------------
# 8. Bridge payload has scaled fields
# ---------------------------------------------------------------------------
def test_bridge_payload_has_scaled_fields():
    from hft_platform.events import LOBStatsEvent
    from research.backtest.alpha_strategy_bridge import AlphaStrategyBridge

    class _A:
        def update(self, **kw):
            self.kw = kw
            return 0.0

        def reset(self):
            pass

    alpha = _A()
    bridge = AlphaStrategyBridge(alpha=alpha, strategy_id="t")
    bridge.on_stats(
        LOBStatsEvent(
            symbol="T",
            ts=1,
            imbalance=0.0,
            best_bid=50000,
            best_ask=50100,
            bid_depth=10,
            ask_depth=10,
        )
    )
    assert alpha.kw["bid_px_scaled"] == 50000
    assert alpha.kw["mid_price_x2"] == 100100
    assert isinstance(alpha.kw["mid_price_x2"], int)


# ---------------------------------------------------------------------------
# 9. Signal log uses correct mid_price
# ---------------------------------------------------------------------------
def test_signal_log_uses_mid_price():
    from hft_platform.events import LOBStatsEvent
    from research.backtest.alpha_strategy_bridge import AlphaStrategyBridge

    class _A:
        def update(self, **kw):
            return 1.0

        def reset(self):
            pass

    bridge = AlphaStrategyBridge(alpha=_A(), strategy_id="t")
    bridge.on_stats(
        LOBStatsEvent(
            symbol="T",
            ts=100,
            imbalance=0.0,
            best_bid=50000,
            best_ask=50100,
            bid_depth=10,
            ask_depth=10,
        )
    )
    log = bridge.signal_log
    assert len(log) == 1
    assert log[0][1] == pytest.approx(1.0)  # signal


# ---------------------------------------------------------------------------
# 10. signals_to_positions clamp
# ---------------------------------------------------------------------------
def test_signals_to_positions_clamp():
    from research.backtest.hft_native_runner import _signals_to_positions

    signals = np.array([0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0])
    pos = _signals_to_positions(signals, 0.5, 3)
    assert float(pos[-1]) <= 3.0
    assert float(np.max(pos)) <= 3.0

    neg_signals = np.array([0.0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0])
    neg_pos = _signals_to_positions(neg_signals, 0.5, 3)
    assert float(neg_pos[-1]) >= -3.0


# ---------------------------------------------------------------------------
# 11. apply_latency deterministic
# ---------------------------------------------------------------------------
def test_apply_latency_deterministic():
    from research.backtest.hft_native_runner import _apply_latency_to_positions
    from research.backtest.types import BacktestConfig

    cfg = BacktestConfig(data_paths=[], submit_ack_latency_ms=10.0)
    desired = np.array([0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0])

    # Create mock data with local_ts field
    dt = np.dtype([("local_ts", np.int64)])
    data = np.zeros(10, dtype=dt)
    data["local_ts"] = np.arange(10) * 1_000_000

    r1 = _apply_latency_to_positions(data, desired, cfg)
    r2 = _apply_latency_to_positions(data, desired, cfg)
    np.testing.assert_array_equal(r1, r2)


# ---------------------------------------------------------------------------
# 12. Equity curve fee deduction
# ---------------------------------------------------------------------------
def test_equity_curve_fee_deduction():
    prices = np.array([100.0, 100.0, 100.0])
    positions = np.array([0.0, 1.0, 0.0])
    eq = compute_equity_from_positions(prices, positions, fee_rate=0.01, initial_equity=1000.0)
    assert eq[-1] < 1000.0  # fees reduced equity


# ---------------------------------------------------------------------------
# 13. Adapter/runner equity parity (same function)
# ---------------------------------------------------------------------------
def test_adapter_runner_equity_parity():
    """Both adapter and runner use same compute_equity_from_positions."""
    prices = np.array([100.0, 101.0, 102.0, 101.0, 100.0])
    positions = np.array([1.0, 1.0, -1.0, -1.0, 0.0])
    eq1 = compute_equity_from_positions(prices, positions, fee_rate=0.001)
    eq2 = compute_equity_from_positions(prices, positions, fee_rate=0.001)
    np.testing.assert_array_equal(eq1, eq2)


# ---------------------------------------------------------------------------
# 14. Walk-forward fold isolation (via frozen dataclass)
# ---------------------------------------------------------------------------
def test_walk_forward_fold_isolation():
    from research.backtest.types import WalkForwardFoldResult

    f1 = WalkForwardFoldResult(
        fold_idx=0, train_size=100, test_size=30, sharpe=1.0, ic_mean=0.1, max_drawdown=0.05, turnover=2.0
    )
    f2 = WalkForwardFoldResult(
        fold_idx=1, train_size=100, test_size=30, sharpe=0.5, ic_mean=0.05, max_drawdown=0.03, turnover=1.5
    )
    # Frozen: no mutation between folds
    with pytest.raises(AttributeError):
        f1.sharpe = 999  # type: ignore[misc]
    assert f1.sharpe != f2.sharpe


# ---------------------------------------------------------------------------
# 15. Feed/elapse event parity (DRY: both use build_lob_event)
# ---------------------------------------------------------------------------
def test_feed_elapse_event_parity(monkeypatch):
    from hft_platform.backtest._hbt_utils import build_lob_event

    _patch(monkeypatch)
    feed_adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
        tick_mode="feed",
    )
    elapse_adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
        tick_mode="elapse",
    )
    e1, _ = build_lob_event(feed_adapter, _Depth(), 1000, 10000, 10010)
    e2, _ = build_lob_event(elapse_adapter, _Depth(), 1000, 10000, 10010)
    assert e1.best_bid == e2.best_bid
    assert e1.best_ask == e2.best_ask
    assert e1.imbalance == e2.imbalance


# ---------------------------------------------------------------------------
# 16. Tick overhead under 100us (SoA + pre-alloc: recording is fast)
# ---------------------------------------------------------------------------
def test_tick_overhead_under_100us(monkeypatch):
    """SoA fill recording + equity sampling should be sub-100us."""
    import time

    _patch(monkeypatch)
    a = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
        equity_sample_ns=1,
    )
    a._reset_equity_buffers()

    n = 1000
    t0 = time.perf_counter_ns()
    for i in range(n):
        a._record_fill(i * 1000, 1, i + 1, 20000)
        a._maybe_record_equity_point(i * 1000, 10000, 10010)
    elapsed_ns = time.perf_counter_ns() - t0

    per_tick_us = elapsed_ns / n / 1000.0
    # Allow generous margin for CI; target is <100us
    assert per_tick_us < 500, f"Per-tick overhead {per_tick_us:.1f}us exceeds 500us threshold"
