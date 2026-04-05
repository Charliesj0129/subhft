"""WU-04: Tests for float price elimination (Precision Law compliance)."""

from __future__ import annotations

import numpy as np
import pytest

from hft_platform.backtest import adapter as hbt_adapter
from hft_platform.strategy.base import BaseStrategy


# ---------------------------------------------------------------------------
# Shared mocks
# ---------------------------------------------------------------------------
class _Depth:
    # hftbacktest returns float prices (descaled by x10000 from platform convention).
    # E.g., platform price 100_000_000 (10000.0000 NTD x10000) → hbt float 10000.0
    best_bid = 1.0
    best_ask = 1.001


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
def test_mid_price_x2_is_integer(monkeypatch):
    """get_mid_price_x2() returns int, not float."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    result = adapter.get_mid_price_x2()
    assert isinstance(result, int)
    # hbt float 1.0 * 10000 = 10000, hbt float 1.001 * 10000 = 10010 → sum = 20010
    assert result == 20010


def test_fill_stores_mid_price_x2_int(monkeypatch):
    """_record_fill stores mid_price_x2 as int64."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    adapter._record_fill(100, 1, 1, 20010)
    assert adapter._fill_mid_price_x2.dtype == np.int64
    assert adapter._fill_mid_price_x2[0] == 20010


def test_equity_uses_mid_price_x2(monkeypatch):
    """_maybe_record_equity_point uses integer mid_price_x2 arithmetic."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
        equity_sample_ns=1,
    )
    adapter._reset_equity_buffers()
    # With position=0, equity = balance + 0 * mid = balance
    adapter._maybe_record_equity_point(100, 10000, 10010)
    assert adapter._equity_count == 1
    # balance is initial_balance (1_000_000.0 default)
    assert adapter._equity_val_buf[0] == pytest.approx(1_000_000.0)


def test_backward_compat_mid_price_float(monkeypatch):
    """_fill_log backward-compat dict has float mid_price derived from int mid_price_x2."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    adapter._record_fill(100, 1, 1, 20010)
    log = adapter._fill_log
    assert log[0]["mid_price"] == pytest.approx(10005.0)
    assert log[0]["mid_price_x2"] == 20010


def test_bridge_payload_has_scaled_fields():
    """AlphaStrategyBridge payload includes bid_px, ask_px, current_mid as floats."""
    from hft_platform.events import LOBStatsEvent

    class _DummyAlpha:
        def update(self, **kw):
            self.last_payload = kw
            return 0.0

        def reset(self):
            pass

    from research.backtest.alpha_strategy_bridge import AlphaStrategyBridge

    alpha = _DummyAlpha()
    bridge = AlphaStrategyBridge(alpha=alpha, strategy_id="test")

    event = LOBStatsEvent(
        symbol="TEST",
        ts=1000,
        imbalance=0.0,
        best_bid=50000,
        best_ask=50100,
        bid_depth=10,
        ask_depth=10,
    )
    bridge.on_stats(event)

    payload = alpha.last_payload
    assert "bid_px" in payload
    assert "ask_px" in payload
    assert "current_mid" in payload
    assert payload["bid_px"] == 50000 / 10000  # 5.0
    assert payload["ask_px"] == 50100 / 10000  # 5.01
    assert payload["current_mid"] == (5.0 + 5.01) / 2.0


def test_signal_log_still_records(monkeypatch):
    """Signal log still records after WU-04 changes."""
    from hft_platform.events import LOBStatsEvent
    from research.backtest.alpha_strategy_bridge import AlphaStrategyBridge

    class _Alpha:
        def update(self, **kw):
            return 0.5

        def reset(self):
            pass

    bridge = AlphaStrategyBridge(alpha=_Alpha(), strategy_id="test")
    event = LOBStatsEvent(
        symbol="TEST",
        ts=1000,
        imbalance=0.0,
        best_bid=50000,
        best_ask=50100,
        bid_depth=10,
        ask_depth=10,
    )
    bridge.on_stats(event)

    log = bridge.signal_log
    assert len(log) == 1
    assert log[0][1] == pytest.approx(0.5)
