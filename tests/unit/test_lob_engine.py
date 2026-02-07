import time

import numpy as np
import pytest

from hft_platform.events import BidAskEvent, MetaData, TickEvent
from hft_platform.feed_adapter.lob_engine import LOBEngine


@pytest.fixture
def engine():
    return LOBEngine()


def make_meta(ts=0):
    return MetaData(seq=1, topic="test", source_ts=ts, local_ts=time.time_ns())


def test_bidask_update(engine):
    bids = np.array([[5000000, 10]], dtype=np.int64)
    asks = np.array([[5010000, 20]], dtype=np.int64)
    event = BidAskEvent(meta=make_meta(1000), symbol="2330", bids=bids, asks=asks, is_snapshot=True)

    stats = engine.process_event(event)
    assert stats.symbol == "2330"
    assert stats.best_bid == 5000000
    assert stats.best_ask == 5010000
    assert stats.mid_price_x2 == 10010000  # best_bid + best_ask
    assert stats.spread_scaled == 10000  # best_ask - best_bid
    assert stats.bid_depth == 10
    assert stats.ask_depth == 20
    assert stats.imbalance == pytest.approx(-0.3333333)


def test_incremental_update(engine):
    engine.process_event(
        BidAskEvent(
            meta=make_meta(1000),
            symbol="2330",
            bids=np.array([[100, 10]], dtype=np.int64),
            asks=np.array([[102, 10]], dtype=np.int64),
        )
    )
    event = BidAskEvent(
        meta=make_meta(1001),
        symbol="2330",
        bids=np.array([[101, 5]], dtype=np.int64),
        asks=np.array([[102, 10]], dtype=np.int64),
    )
    stats = engine.process_event(event)

    assert stats.best_bid == 101
    assert stats.mid_price_x2 == 203  # 101 + 102
    assert stats.spread_scaled == 1  # 102 - 101
    assert stats.bid_depth == 5


def test_tick_update(engine):
    engine.process_event(
        BidAskEvent(
            meta=make_meta(1000),
            symbol="2330",
            bids=np.array([[100, 10]], dtype=np.int64),
            asks=np.array([[102, 10]], dtype=np.int64),
        )
    )
    tick = TickEvent(
        meta=make_meta(1005),
        symbol="2330",
        price=101,
        volume=2,
        total_volume=100,
        bid_side_total_vol=0,
        ask_side_total_vol=0,
        is_simtrade=False,
        is_odd_lot=False,
    )
    stats = engine.process_event(tick)
    assert stats is None
    book = engine.get_book("2330")
    assert book.last_price == 101


def test_missing_symbol_snapshot(engine):
    snap = engine.get_book_snapshot("UNKNOWN")
    assert snap is None


def test_valid_snapshot(engine):
    engine.process_event(
        BidAskEvent(
            meta=make_meta(1000),
            symbol="2330",
            bids=np.array([[100, 10]], dtype=np.int64),
            asks=np.array([[102, 10]], dtype=np.int64),
        )
    )
    snap = engine.get_book_snapshot("2330")
    assert snap is not None
    assert snap["symbol"] == "2330"
    assert snap["best_bid"] == 100
    assert snap["best_ask"] == 102


def test_empty_update(engine):
    # Should clear book if empty list passed? Or just not crash?
    # Code says: if bids: self.bids = bids; else: self.bids = []
    # So it clears.
    engine.process_event(BidAskEvent(meta=make_meta(1000), symbol="2330", bids=[[100, 1]], asks=[[102, 1]]))
    stats = engine.process_event(BidAskEvent(meta=make_meta(1001), symbol="2330", bids=[], asks=[]))
    assert stats.mid_price == 0.0  # Empty book, backward-compat field
    assert stats.bid_depth == 0


def test_late_packet_handling(engine):
    # 1. Update with ts=2000
    engine.process_event(BidAskEvent(meta=make_meta(2000), symbol="2330", bids=[[100, 1]], asks=[[102, 1]]))
    book = engine.get_book("2330")
    assert book.exch_ts == 2000
    assert book.mid_price_x2 / 2.0 == 101.0

    # 2. Late update ts=1000 with DIFFERENT price
    # Should be IGNORED
    engine.process_event(BidAskEvent(meta=make_meta(1000), symbol="2330", bids=[[50, 1]], asks=[[52, 1]]))

    # 3. Verify state UNCHANGED
    assert book.exch_ts == 2000
    assert book.mid_price_x2 / 2.0 == 101.0  # If bug exists, this might fail (be 51.0)


from unittest.mock import MagicMock


def test_tick_late(engine):
    engine.process_event(BidAskEvent(meta=make_meta(1000), symbol="A", bids=[[100, 10]], asks=[[102, 10]]))
    # Late tick
    engine.process_event(
        TickEvent(
            meta=make_meta(900),
            symbol="A",
            price=99,
            volume=1,
            total_volume=0,
            bid_side_total_vol=0,
            ask_side_total_vol=0,
            is_simtrade=False,
            is_odd_lot=False,
        )
    )

    book = engine.get_book("A")
    assert book.last_price == 0  # Should not update


def test_metrics_emit(engine):
    engine.metrics = MagicMock()
    engine.process_event(BidAskEvent(meta=make_meta(1001), symbol="A", bids=[[100, 1]], asks=[[101, 1]]))
    # Verify metric call
    # self.metrics.lob_updates_total.labels(symbol=..., type="BidAsk").inc()
    engine.metrics.lob_updates_total.labels.assert_called()
    engine.metrics.lob_updates_total.labels.return_value.inc.assert_called()
