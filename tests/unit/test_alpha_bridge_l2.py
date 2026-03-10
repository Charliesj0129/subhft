"""Tests for AlphaStrategyBridge L2 depth payload pass-through."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from hft_platform.events import BidAskEvent, LOBStatsEvent, MetaData
from research.backtest.alpha_strategy_bridge import AlphaStrategyBridge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_meta() -> MetaData:
    return MetaData(seq=0, source_ts=1_000_000, local_ts=1_000_100)


def _make_bidask(
    symbol: str = "2330",
    n_levels: int = 5,
) -> BidAskEvent:
    """Create a BidAskEvent with N price levels."""
    bids = np.array(
        [[1000_0000 - i * 1_0000, 100 + i] for i in range(n_levels)],
        dtype=np.int64,
    )
    asks = np.array(
        [[1000_0000 + (i + 1) * 1_0000, 200 + i] for i in range(n_levels)],
        dtype=np.int64,
    )
    return BidAskEvent(
        meta=_make_meta(),
        symbol=symbol,
        bids=bids,
        asks=asks,
    )


def _make_stats(symbol: str = "2330", ts: int = 1_000_000) -> LOBStatsEvent:
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=0.1,
        best_bid=999_0000,
        best_ask=1001_0000,
        bid_depth=500,
        ask_depth=600,
    )


def _make_bridge(alpha: Any, symbol: str = "2330") -> AlphaStrategyBridge:
    return AlphaStrategyBridge(alpha, symbol=symbol)


class _StubAlpha:
    """Minimal alpha that records update kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.manifest = MagicMock()

    def reset(self) -> None:
        self.calls.clear()

    def update(self, **kwargs: Any) -> float:
        self.calls.append(kwargs)
        return 0.5


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBidAskCaching:
    """BidAskEvent caching behaviour."""

    def test_bidask_returns_empty_intents(self) -> None:
        alpha = _StubAlpha()
        bridge = _make_bridge(alpha)
        ctx = MagicMock()

        result = bridge.handle_event(ctx, _make_bidask())

        assert result == []
        assert len(alpha.calls) == 0  # alpha.update NOT called

    def test_bidask_cached(self) -> None:
        alpha = _StubAlpha()
        bridge = _make_bridge(alpha)
        ctx = MagicMock()

        ba = _make_bidask()
        bridge.handle_event(ctx, ba)

        assert bridge._last_bidask is ba

    def test_bidask_wrong_symbol_not_cached(self) -> None:
        alpha = _StubAlpha()
        bridge = _make_bridge(alpha, symbol="2330")
        ctx = MagicMock()

        bridge.handle_event(ctx, _make_bidask(symbol="2317"))

        assert bridge._last_bidask is None

    def test_bidask_no_symbol_filter_caches_all(self) -> None:
        alpha = _StubAlpha()
        bridge = _make_bridge(alpha, symbol="")
        ctx = MagicMock()

        ba = _make_bidask(symbol="ANY")
        bridge.handle_event(ctx, ba)

        assert bridge._last_bidask is ba


class TestL2Payload:
    """L2 arrays are included in alpha.update() payload."""

    def test_l2_arrays_passed_to_alpha(self) -> None:
        alpha = _StubAlpha()
        bridge = _make_bridge(alpha)
        ctx = MagicMock()

        ba = _make_bidask(n_levels=5)
        bridge.handle_event(ctx, ba)
        bridge.handle_event(ctx, _make_stats())

        assert len(alpha.calls) == 1
        kw = alpha.calls[0]
        np.testing.assert_array_equal(kw["bids"], ba.bids)
        np.testing.assert_array_equal(kw["asks"], ba.asks)

    def test_l1_arrays_passed_to_alpha(self) -> None:
        alpha = _StubAlpha()
        bridge = _make_bridge(alpha)
        ctx = MagicMock()

        ba = _make_bidask(n_levels=1)
        bridge.handle_event(ctx, ba)
        bridge.handle_event(ctx, _make_stats())

        kw = alpha.calls[0]
        assert kw["bids"].shape == (1, 2)
        assert kw["asks"].shape == (1, 2)

    def test_no_bidask_no_depth_keys(self) -> None:
        """Backward compat: stats-only mode has no bids/asks in payload."""
        alpha = _StubAlpha()
        bridge = _make_bridge(alpha)
        ctx = MagicMock()

        bridge.handle_event(ctx, _make_stats())

        kw = alpha.calls[0]
        assert "bids" not in kw
        assert "asks" not in kw

    def test_l2_shape_5x2(self) -> None:
        alpha = _StubAlpha()
        bridge = _make_bridge(alpha)
        ctx = MagicMock()

        bridge.handle_event(ctx, _make_bidask(n_levels=5))
        bridge.handle_event(ctx, _make_stats())

        kw = alpha.calls[0]
        assert kw["bids"].shape == (5, 2)
        assert kw["asks"].shape == (5, 2)


class TestResetClearsState:
    """reset() clears cached BidAskEvent."""

    def test_reset_clears_last_bidask(self) -> None:
        alpha = _StubAlpha()
        bridge = _make_bridge(alpha)
        ctx = MagicMock()

        bridge.handle_event(ctx, _make_bidask())
        assert bridge._last_bidask is not None

        bridge.reset()
        assert bridge._last_bidask is None

    def test_after_reset_no_depth_in_payload(self) -> None:
        alpha = _StubAlpha()
        bridge = _make_bridge(alpha)
        ctx = MagicMock()

        bridge.handle_event(ctx, _make_bidask())
        bridge.reset()
        bridge.handle_event(ctx, _make_stats())

        kw = alpha.calls[0]
        assert "bids" not in kw
        assert "asks" not in kw
