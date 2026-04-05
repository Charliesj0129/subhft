"""Tests that event dataclasses are frozen (immutable after construction).

Prevents cross-strategy data corruption when the same event reference
is dispatched to multiple strategies in StrategyRunner.
"""

import numpy as np
import pytest

from hft_platform.events import BidAskEvent, LOBStatsEvent, MetaData, TickEvent


def _make_meta() -> MetaData:
    return MetaData(seq=1, source_ts=1000, local_ts=2000, topic="test")


class TestMetaDataFrozen:
    def test_cannot_assign_field(self) -> None:
        meta = _make_meta()
        with pytest.raises(AttributeError):
            meta.seq = 99  # type: ignore[misc]

    def test_cannot_assign_topic(self) -> None:
        meta = _make_meta()
        with pytest.raises(AttributeError):
            meta.topic = "mutated"  # type: ignore[misc]


class TestTickEventFrozen:
    def test_cannot_assign_price(self) -> None:
        tick = TickEvent(meta=_make_meta(), symbol="2330", price=100_0000, volume=10)
        with pytest.raises(AttributeError):
            tick.price = 200_0000  # type: ignore[misc]

    def test_cannot_assign_volume(self) -> None:
        tick = TickEvent(meta=_make_meta(), symbol="2330", price=100_0000, volume=10)
        with pytest.raises(AttributeError):
            tick.volume = 999  # type: ignore[misc]

    def test_cannot_assign_symbol(self) -> None:
        tick = TickEvent(meta=_make_meta(), symbol="2330", price=100_0000, volume=10)
        with pytest.raises(AttributeError):
            tick.symbol = "AAPL"  # type: ignore[misc]


class TestBidAskEventFrozen:
    def test_cannot_assign_bids(self) -> None:
        bids = np.array([[100_0000, 10]], dtype=np.int64)
        asks = np.array([[101_0000, 5]], dtype=np.int64)
        event = BidAskEvent(meta=_make_meta(), symbol="2330", bids=bids, asks=asks)
        with pytest.raises(AttributeError):
            event.bids = np.array([[200_0000, 1]], dtype=np.int64)  # type: ignore[misc]

    def test_cannot_assign_is_snapshot(self) -> None:
        bids = np.array([[100_0000, 10]], dtype=np.int64)
        asks = np.array([[101_0000, 5]], dtype=np.int64)
        event = BidAskEvent(meta=_make_meta(), symbol="2330", bids=bids, asks=asks)
        with pytest.raises(AttributeError):
            event.is_snapshot = True  # type: ignore[misc]

    def test_construction_with_is_snapshot(self) -> None:
        bids = np.array([[100_0000, 10]], dtype=np.int64)
        asks = np.array([[101_0000, 5]], dtype=np.int64)
        event = BidAskEvent(
            meta=_make_meta(), symbol="2330", bids=bids, asks=asks, is_snapshot=True
        )
        assert event.is_snapshot is True


class TestLOBStatsEventFrozen:
    def test_cannot_assign_imbalance(self) -> None:
        event = LOBStatsEvent(
            symbol="2330", ts=1000, imbalance=0.5, best_bid=100, best_ask=102,
            bid_depth=10, ask_depth=8,
        )
        with pytest.raises(AttributeError):
            event.imbalance = 0.9  # type: ignore[misc]

    def test_cannot_assign_mid_price_x2(self) -> None:
        event = LOBStatsEvent(
            symbol="2330", ts=1000, imbalance=0.5, best_bid=100, best_ask=102,
            bid_depth=10, ask_depth=8,
        )
        with pytest.raises(AttributeError):
            event.mid_price_x2 = 999  # type: ignore[misc]

    def test_post_init_computes_mid_price_x2(self) -> None:
        event = LOBStatsEvent(
            symbol="2330", ts=1000, imbalance=0.5, best_bid=100, best_ask=102,
            bid_depth=10, ask_depth=8,
        )
        assert event.mid_price_x2 == 202
        assert event.spread_scaled == 2

    def test_post_init_uses_provided_values(self) -> None:
        event = LOBStatsEvent(
            symbol="2330", ts=1000, imbalance=0.5, best_bid=100, best_ask=102,
            bid_depth=10, ask_depth=8, mid_price_x2=999, spread_scaled=777,
        )
        assert event.mid_price_x2 == 999
        assert event.spread_scaled == 777

    def test_properties_still_work(self) -> None:
        event = LOBStatsEvent(
            symbol="2330", ts=1000, imbalance=0.5, best_bid=100, best_ask=102,
            bid_depth=10, ask_depth=8,
        )
        assert event.mid_price == 101.0
        assert event.spread == 2.0
        assert event.mid_price_scaled == 101
