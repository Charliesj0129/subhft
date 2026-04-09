"""
Tests verifying that LOBEngine and FeatureEngine no longer return shared mutable
object references across consecutive ticks.

Problem fixed: Both BookState.get_stats() and FeatureEngine.update() previously
returned the same object mutated in-place each tick, causing data corruption when
multiple consumers (StrategyRunner, RecorderService, FeatureEngine) held references.
"""

from __future__ import annotations

import numpy as np
import pytest

from hft_platform.events import LOBStatsEvent
from hft_platform.feature.engine import FeatureEngine
from hft_platform.feed_adapter.lob_engine import BookState

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_book_state(symbol: str = "2330") -> BookState:
    bs = BookState(symbol)
    bs.bids = np.array([[1000000, 100], [999000, 200]], dtype=np.int64)
    bs.asks = np.array([[1001000, 80], [1002000, 150]], dtype=np.int64)
    bs.mid_price_x2 = 2001000
    bs.spread = 1000
    bs.imbalance = 0.1
    bs.exch_ts = 1_000_000_000
    bs.bid_depth_total = 300
    bs.ask_depth_total = 230
    return bs


def _lob_stats(
    symbol: str = "2330",
    ts: int = 1,
    bid: int = 1_000_000,
    ask: int = 1_001_000,
    bq: int = 10,
    aq: int = 20,
) -> LOBStatsEvent:
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=0.0,
        best_bid=bid,
        best_ask=ask,
        bid_depth=bq,
        ask_depth=aq,
    )


# ── BookState.get_stats() — object identity ───────────────────────────────────


class TestBookStateGetStatsObjectIdentity:
    def test_consecutive_calls_return_different_objects(self):
        """Two consecutive get_stats() calls must return distinct objects."""
        bs = _make_book_state()
        first = bs.get_stats()
        second = bs.get_stats()
        assert first is not second, (
            "get_stats() returned the same object reference on consecutive calls; "
            "shared mutable state causes data corruption across consumers."
        )

    def test_mutation_of_returned_object_does_not_affect_next_call(self):
        """LOBStatsEvent is now frozen — mutation raises FrozenInstanceError, preventing cross-consumer corruption."""
        import dataclasses

        bs = _make_book_state()
        first = bs.get_stats()

        # R8: LOBStatsEvent is now frozen=True, so mutation is structurally impossible
        with pytest.raises(dataclasses.FrozenInstanceError):
            first.ts = 999_999_999_999

        # The next tick updates the book state timestamp
        bs.exch_ts = 2_000_000_000
        second = bs.get_stats()

        assert second.ts == 2_000_000_000, "next get_stats() returned stale/corrupted ts."

    def test_returned_event_fields_match_book_state(self):
        """get_stats() fields must reflect the current BookState values."""
        bs = _make_book_state()
        bs.exch_ts = 42
        bs.mid_price_x2 = 2_000_500
        bs.spread = 500
        bs.imbalance = -0.3

        evt = bs.get_stats()

        assert evt.symbol == "2330"
        assert evt.ts == 42
        assert evt.mid_price_x2 == 2_000_500
        assert evt.spread_scaled == 500
        assert evt.imbalance == pytest.approx(-0.3)

    def test_multiple_consumers_see_independent_snapshots(self):
        """Simulate multiple consumers holding different tick snapshots."""
        bs = _make_book_state()
        bs.exch_ts = 100
        snap1 = bs.get_stats()

        # Simulate next tick
        bs.exch_ts = 200
        bs.spread = 2000
        snap2 = bs.get_stats()

        # Each snapshot must be independent
        assert snap1 is not snap2
        assert snap1.ts == 100
        assert snap2.ts == 200
        assert snap1.spread_scaled != snap2.spread_scaled


# ── FeatureEngine — object identity ──────────────────────────────────────────


class TestFeatureEngineObjectIdentity:
    def test_consecutive_updates_return_different_objects(self):
        """Two consecutive process_lob_stats() calls must return distinct FeatureUpdateEvent objects."""
        eng = FeatureEngine()
        stats1 = _lob_stats(ts=1_000)
        stats2 = _lob_stats(ts=2_000)

        first = eng.process_lob_stats(stats1, local_ts_ns=1_000)
        second = eng.process_lob_stats(stats2, local_ts_ns=2_000)

        assert first is not None
        assert second is not None
        assert first is not second, (
            "process_lob_stats() returned the same FeatureUpdateEvent on consecutive "
            "calls; shared mutable state causes data corruption across consumers."
        )

    def test_mutation_of_returned_event_does_not_affect_next_update(self):
        """Mutating a returned FeatureUpdateEvent must not corrupt the next emission."""
        eng = FeatureEngine()
        first = eng.process_lob_stats(_lob_stats(ts=1_000), local_ts_ns=1_000)
        assert first is not None

        # Consumer mutates the returned event (e.g., tagging for later use)
        first.ts = 999_999

        second = eng.process_lob_stats(_lob_stats(ts=2_000), local_ts_ns=2_000)
        assert second is not None

        assert second.ts == 2_000, (
            "next FeatureUpdateEvent has corrupted ts; consumer mutation of the first event bled through."
        )
        # First event retains the consumer's mutation
        assert first.ts == 999_999

    def test_multiple_symbols_return_independent_events(self):
        """Each symbol's FeatureUpdateEvent must be independent."""
        eng = FeatureEngine()
        stats_a = _lob_stats(symbol="2330", ts=1_000)
        stats_b = _lob_stats(symbol="2317", ts=1_000)

        evt_a = eng.process_lob_stats(stats_a, local_ts_ns=1_000)
        evt_b = eng.process_lob_stats(stats_b, local_ts_ns=1_000)

        assert evt_a is not None
        assert evt_b is not None
        assert evt_a is not evt_b
        assert evt_a.symbol == "2330"
        assert evt_b.symbol == "2317"

    def test_same_symbol_consecutive_ticks_are_independent_objects(self):
        """The same symbol on consecutive ticks must produce independent event objects."""
        eng = FeatureEngine()
        events = [eng.process_lob_stats(_lob_stats(ts=i * 1000), local_ts_ns=i * 1000) for i in range(1, 4)]
        # All events must be non-None
        assert all(e is not None for e in events)
        # All events must be distinct objects
        for i in range(len(events)):
            for j in range(i + 1, len(events)):
                assert events[i] is not events[j], (
                    f"events[{i}] and events[{j}] are the same object; shared mutable ref detected."
                )

    def test_event_cache_retains_last_event_for_debugging(self):
        """_event_cache should store the last emitted event per symbol (for debugging)."""
        eng = FeatureEngine()
        last = eng.process_lob_stats(_lob_stats(symbol="2330", ts=5_000), local_ts_ns=5_000)
        assert last is not None
        # The cache should hold the last emitted event for this symbol
        cached = eng._event_cache.get("2330")
        assert cached is last, (
            "_event_cache should store (not replace with a fresh copy) the last emitted event for debugging purposes."
        )
