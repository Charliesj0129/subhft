"""Tests for lobstats tuple tag prepend (DATA-02 fix).

Verifies:
1. BookState.get_stats_tuple() returns tuple starting with "lobstats"
2. _StatsTupleProxy correctly reads fields from tagged tuple
3. StrategyRunner tuple guard accepts lobstats tuples
4. Stats tuple reaches strategy on_stats() callback
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from hft_platform.feed_adapter import lob_engine as lob_mod
from hft_platform.feed_adapter.lob_engine import BookState
from hft_platform.feature.engine import _StatsTupleProxy
from hft_platform.strategy.base import BaseStrategy, StrategyContext
from hft_platform.strategy.runner import _KNOWN_TUPLE_TAGS


# ---------------------------------------------------------------------------
# 1. BookState.get_stats_tuple() tag
# ---------------------------------------------------------------------------


class TestGetStatsTupleTag:
    """get_stats_tuple() must return a tuple starting with 'lobstats'."""

    def test_python_path_has_lobstats_tag(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_RustBookState", None)
        monkeypatch.setattr(lob_mod, "_RUST_BOOK_STATE_ENABLED", False)
        b = BookState("TEST")
        b.apply_update(
            np.array([[1000000, 10]], dtype=np.int64),
            np.array([[1010000, 5]], dtype=np.int64),
            1_000_000_000,
        )
        t = b.get_stats_tuple()
        assert t[0] == "lobstats"
        assert t[1] == "TEST"
        assert len(t) == 10

    def test_rust_path_prepends_lobstats_tag(self):
        bs = BookState("SYM")
        mock_rs = MagicMock()
        rust_tuple = ("SYM", 100, 2000, 10, 0.5, 1000, 1010, 50, 30)
        mock_rs.get_stats_tuple.return_value = rust_tuple
        bs._rust_state = mock_rs
        t = bs.get_stats_tuple()
        assert t[0] == "lobstats"
        assert t[1:] == rust_tuple
        assert len(t) == 10

    def test_rust_fallback_still_has_tag(self):
        bs = BookState("FB")
        mock_rs = MagicMock()
        mock_rs.get_stats_tuple.side_effect = RuntimeError("boom")
        bs._rust_state = mock_rs
        bs.bids = np.array([[500000, 10]], dtype=np.int64)
        bs.asks = np.array([[510000, 5]], dtype=np.int64)
        bs.mid_price_x2 = 1010000
        t = bs.get_stats_tuple()
        assert t[0] == "lobstats"


# ---------------------------------------------------------------------------
# 2. _StatsTupleProxy with tagged tuple
# ---------------------------------------------------------------------------


class TestStatsTupleProxyTagged:
    """_StatsTupleProxy must correctly access fields at shifted indices."""

    def test_all_fields(self):
        t = ("lobstats", "TXFD6", 999, 400000, 10000, 0.6, 200000, 210000, 50, 30)
        proxy = _StatsTupleProxy(t)
        assert proxy.symbol == "TXFD6"
        assert proxy.ts == 999
        assert proxy.mid_price_x2 == 400000
        assert proxy.spread_scaled == 10000
        assert proxy.imbalance == pytest.approx(0.6)
        assert proxy.best_bid == 200000
        assert proxy.best_ask == 210000
        assert proxy.bid_depth == 50
        assert proxy.ask_depth == 30


# ---------------------------------------------------------------------------
# 3. Runner tuple guard accepts lobstats
# ---------------------------------------------------------------------------


class TestRunnerTupleGuard:
    """The runner must accept lobstats-tagged tuples (not drop them)."""

    def test_lobstats_in_known_tags(self):
        assert "lobstats" in _KNOWN_TUPLE_TAGS

    def test_tagged_tuple_passes_guard(self):
        event = ("lobstats", "SYM", 1, 2000, 10, 0.5, 1000, 1010, 50, 30)
        # Guard logic: skip if event[0] not in _KNOWN_TUPLE_TAGS
        assert event[0] in _KNOWN_TUPLE_TAGS

    def test_untagged_symbol_would_be_dropped(self):
        event = ("SYM", 1, 2000, 10, 0.5, 1000, 1010, 50, 30)
        assert event[0] not in _KNOWN_TUPLE_TAGS


# ---------------------------------------------------------------------------
# 4. Stats tuple reaches strategy on_stats()
# ---------------------------------------------------------------------------


class TestStrategyOnStatsDispatch:
    """A lobstats tuple must reach BaseStrategy.on_stats() via handle_event."""

    def test_lobstats_tuple_dispatches_to_on_stats(self):
        class Spy(BaseStrategy):
            def __init__(self):
                super().__init__(strategy_id="spy", symbols=[])
                self.stats_received = []

            def on_stats(self, event):
                self.stats_received.append(event)

        spy = Spy()
        ctx = MagicMock(spec=StrategyContext)
        ctx.positions = {}
        ctx.strategy_id = "spy"

        stats_tuple = ("lobstats", "TXFD6", 1, 2000, 10, 0.5, 1000, 1010, 50, 30)
        spy.handle_event(ctx, stats_tuple)

        assert len(spy.stats_received) == 1
        assert spy.stats_received[0] is stats_tuple
