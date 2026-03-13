"""Unit tests for research/backtest/alpha_strategy_bridge.py."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from research.backtest.alpha_strategy_bridge import (
    AlphaStrategyBridge,
    signal_log_to_arrays,
)


# ---------------------------------------------------------------------------
# Minimal AlphaProtocol stub
# ---------------------------------------------------------------------------
class _ConstantAlpha:
    """Always returns a fixed signal value."""

    def __init__(self, value: float = 0.5):
        self._value = value
        self.reset_called = 0

    @property
    def manifest(self):
        m = MagicMock()
        m.alpha_id = "constant"
        m.data_fields = ("bid_px", "ask_px")
        return m

    def reset(self):
        self.reset_called += 1

    def update(self, **kwargs) -> float:
        return self._value


class _OFICaptureAlpha:
    """Captures kwargs passed to update() so tests can inspect OFI values."""

    def __init__(self):
        self.last_kwargs: dict[str, Any] = {}

    @property
    def manifest(self):
        m = MagicMock()
        m.alpha_id = "ofi_capture"
        m.data_fields = ("bid_px", "ask_px", "ofi_l1_raw", "ofi_l1_cum")
        return m

    def reset(self):
        self.last_kwargs = {}

    def update(self, **kwargs) -> float:
        self.last_kwargs = dict(kwargs)
        return 0.0


class _CountingAlpha:
    """Returns incrementing signal values to track call order."""

    def __init__(self):
        self._count = 0

    @property
    def manifest(self):
        m = MagicMock()
        m.alpha_id = "counter"
        m.data_fields = ("bid_px", "ask_px")
        return m

    def reset(self):
        self._count = 0

    def update(self, **kwargs) -> float:
        self._count += 1
        return float(self._count) * 0.1


# ---------------------------------------------------------------------------
# LOBStatsEvent stub
# ---------------------------------------------------------------------------
def _make_lob_event(
    symbol: str = "TXFB6",
    ts: int = 1_000_000_000,
    best_bid: int = 999_000,  # 99.9 * 10000
    best_ask: int = 1_001_000,  # 100.1 * 10000
    bid_depth: int = 10,
    ask_depth: int = 5,
    imbalance: float = 0.1,
):
    from hft_platform.events import LOBStatsEvent

    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=imbalance,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
    )


# ---------------------------------------------------------------------------
# AlphaStrategyBridge tests
# ---------------------------------------------------------------------------
class TestAlphaStrategyBridgeInit:
    def test_default_params(self):
        alpha = _ConstantAlpha()
        bridge = AlphaStrategyBridge(alpha, symbol="TXFB6")
        assert bridge.max_position == 5
        assert bridge.signal_threshold == pytest.approx(0.3)
        assert bridge._symbol == "TXFB6"
        assert bridge.signal_log == []

    def test_custom_params(self):
        alpha = _ConstantAlpha()
        bridge = AlphaStrategyBridge(alpha, max_position=10, signal_threshold=0.1, symbol="2330")
        assert bridge.max_position == 10
        assert bridge.signal_threshold == pytest.approx(0.1)


class TestAlphaStrategyBridgeReset:
    def test_reset_clears_signal_log(self):
        alpha = _ConstantAlpha(0.5)
        bridge = AlphaStrategyBridge(alpha, symbol="TXFB6")
        event = _make_lob_event()
        bridge.on_stats(event)
        assert len(bridge.signal_log) == 1
        bridge.reset()
        assert bridge.signal_log == []

    def test_reset_calls_alpha_reset(self):
        alpha = _ConstantAlpha()
        bridge = AlphaStrategyBridge(alpha)
        bridge.reset()
        assert alpha.reset_called == 1


class TestAlphaStrategyBridgeOnStats:
    def test_records_signal_log_entry(self):
        alpha = _ConstantAlpha(0.42)
        bridge = AlphaStrategyBridge(alpha, symbol="TXFB6")
        event = _make_lob_event(ts=1_234_567_890, best_bid=999_000, best_ask=1_001_000)
        bridge.on_stats(event)
        assert len(bridge.signal_log) == 1
        ts_ns, signal, mid_price = bridge.signal_log[0]
        assert ts_ns == 1_234_567_890
        assert signal == pytest.approx(0.42)
        # mid = (99.9 + 100.1) / 2 = 100.0
        assert mid_price == pytest.approx(100.0)

    def test_multiple_events_accumulate(self):
        alpha = _CountingAlpha()
        bridge = AlphaStrategyBridge(alpha, symbol="TXFB6")
        for i in range(5):
            event = _make_lob_event(ts=i * 1_000_000)
            bridge.on_stats(event)
        assert len(bridge.signal_log) == 5
        signals = [s for _, s, _ in bridge.signal_log]
        assert signals == pytest.approx([0.1, 0.2, 0.3, 0.4, 0.5])

    def test_price_scaling(self):
        """bid/ask are scaled ints; bridge must divide by price_scale."""
        alpha = _ConstantAlpha(0.0)
        bridge = AlphaStrategyBridge(alpha, symbol="TXFB6", price_scale=10_000)
        # best_bid = 500_000 → 50.0; best_ask = 500_100 → 50.01
        event = _make_lob_event(best_bid=500_000, best_ask=500_100)
        bridge.on_stats(event)
        _, _, mid = bridge.signal_log[0]
        assert mid == pytest.approx((50.0 + 50.01) / 2, rel=1e-4)

    def test_symbol_filter_applied(self):
        """Events for wrong symbol should be ignored."""
        alpha = _ConstantAlpha(0.5)
        bridge = AlphaStrategyBridge(alpha, symbol="TXFB6")
        event = _make_lob_event(symbol="2330")  # different symbol
        bridge.handle_event(MagicMock(), event)
        assert bridge.signal_log == []

    def test_correct_symbol_accepted(self):
        alpha = _ConstantAlpha(0.5)
        bridge = AlphaStrategyBridge(alpha, symbol="TXFB6")
        event = _make_lob_event(symbol="TXFB6")
        bridge.handle_event(MagicMock(), event)
        assert len(bridge.signal_log) == 1

    def test_no_symbol_filter_accepts_all(self):
        """Empty symbol string = accept all."""
        alpha = _ConstantAlpha(0.5)
        bridge = AlphaStrategyBridge(alpha, symbol="")
        event = _make_lob_event(symbol="ANYTHING")
        bridge.handle_event(MagicMock(), event)
        assert len(bridge.signal_log) == 1

    def test_handle_event_returns_empty_intents(self):
        """Bridge never generates order intents."""
        alpha = _ConstantAlpha(0.5)
        bridge = AlphaStrategyBridge(alpha, symbol="TXFB6")
        event = _make_lob_event(symbol="TXFB6")
        ctx = MagicMock()
        intents = bridge.handle_event(ctx, event)
        assert intents == []

    def test_alpha_update_exception_yields_zero_signal(self):
        """Alpha errors should not crash the bridge — yield 0.0 signal."""

        class _BrokenAlpha:
            @property
            def manifest(self):
                m = MagicMock()
                m.data_fields = ()
                return m

            def reset(self):
                pass

            def update(self, **kwargs):
                raise RuntimeError("broken")

        bridge = AlphaStrategyBridge(_BrokenAlpha(), symbol="TXFB6")
        event = _make_lob_event()
        bridge.on_stats(event)
        _, signal, _ = bridge.signal_log[0]
        assert signal == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# signal_log_to_arrays
# ---------------------------------------------------------------------------
class TestSignalLogToArrays:
    def test_empty_log(self):
        ts, sig, mid = signal_log_to_arrays([])
        assert ts.shape == (0,)
        assert sig.shape == (0,)
        assert mid.shape == (0,)

    def test_converts_correctly(self):
        log = [(1_000, 0.5, 100.0), (2_000, -0.3, 100.5), (3_000, 0.0, 101.0)]
        ts, sig, mid = signal_log_to_arrays(log)
        assert ts.dtype == np.int64
        assert sig.dtype == np.float64
        np.testing.assert_array_almost_equal(sig, [0.5, -0.3, 0.0])
        np.testing.assert_array_almost_equal(mid, [100.0, 100.5, 101.0])
        np.testing.assert_array_equal(ts, [1_000, 2_000, 3_000])

    def test_single_entry(self):
        ts, sig, mid = signal_log_to_arrays([(999, 1.23, 50.0)])
        assert float(sig[0]) == pytest.approx(1.23)


# ---------------------------------------------------------------------------
# OFI first-tick spike fix tests
# ---------------------------------------------------------------------------
class TestOFIFirstTickSpike:
    def test_first_tick_ofi_is_zero(self):
        """First tick should produce ofi_l1_raw=0.0, not a spurious spike."""
        alpha = _OFICaptureAlpha()
        bridge = AlphaStrategyBridge(alpha, symbol="TXFB6")
        event = _make_lob_event(bid_depth=100, ask_depth=50)
        bridge.on_stats(event)
        assert alpha.last_kwargs["ofi_l1_raw"] == pytest.approx(0.0)

    def test_second_tick_ofi_computes_delta(self):
        """Second tick should compute proper delta from first tick."""
        alpha = _OFICaptureAlpha()
        bridge = AlphaStrategyBridge(alpha, symbol="TXFB6")
        # First tick: seed prev state
        bridge.on_stats(_make_lob_event(bid_depth=100, ask_depth=50))
        # Second tick: bid 100->120 (+20), ask 50->40 (-10) => ofi = 20 - (-10) = 30
        bridge.on_stats(_make_lob_event(bid_depth=120, ask_depth=40, ts=2_000_000_000))
        assert alpha.last_kwargs["ofi_l1_raw"] == pytest.approx(30.0)

    def test_ofi_cum_no_spike(self):
        """Cumulative OFI should be 0 after first tick, not bid-ask."""
        alpha = _OFICaptureAlpha()
        bridge = AlphaStrategyBridge(alpha, symbol="TXFB6")
        # First tick
        bridge.on_stats(_make_lob_event(bid_depth=100, ask_depth=50))
        assert alpha.last_kwargs["ofi_l1_cum"] == pytest.approx(0.0)
        # Second tick: ofi_raw = (120-100) - (40-50) = 30
        bridge.on_stats(_make_lob_event(bid_depth=120, ask_depth=40, ts=2_000_000_000))
        assert alpha.last_kwargs["ofi_l1_cum"] == pytest.approx(30.0)

    def test_reset_clears_first_tick(self):
        """After reset, next tick should be treated as a new first tick."""
        alpha = _OFICaptureAlpha()
        bridge = AlphaStrategyBridge(alpha, symbol="TXFB6")
        # First tick
        bridge.on_stats(_make_lob_event(bid_depth=100, ask_depth=50))
        assert alpha.last_kwargs["ofi_l1_raw"] == pytest.approx(0.0)
        # Reset
        bridge.reset()
        # Post-reset tick should behave as first tick again
        bridge.on_stats(_make_lob_event(bid_depth=200, ask_depth=80, ts=3_000_000_000))
        assert alpha.last_kwargs["ofi_l1_raw"] == pytest.approx(0.0)
        assert alpha.last_kwargs["ofi_l1_cum"] == pytest.approx(0.0)
