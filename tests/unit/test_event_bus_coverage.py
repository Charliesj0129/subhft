"""Coverage-focused tests for engine/event_bus.py.

Targets _PyFastBidAskRingBuffer branches, _pack_book_levels edge cases,
multi-writer publish_many, and Python-buffer consume paths.
"""

import asyncio
from unittest.mock import MagicMock

from hft_platform.engine import event_bus as event_bus_mod
from hft_platform.engine.event_bus import (
    RingBufferBus,
    _pack_book_levels,
    _PyFastBidAskRingBuffer,
    _PyFastLOBStatsRingBuffer,
    _PyFastTickRingBuffer,
)

# ---------------------------------------------------------------------------
# _pack_book_levels edge cases
# ---------------------------------------------------------------------------


def test_pack_book_levels_returns_empty_for_none():
    result = _pack_book_levels(None)
    assert result == ((), 0)


def test_pack_book_levels_returns_none_for_string():
    result = _pack_book_levels("not a book")
    assert result is None


def test_pack_book_levels_returns_none_for_bytes():
    result = _pack_book_levels(b"bytes")
    assert result is None


def test_pack_book_levels_normal_rows():
    levels = [[10000, 5], [9990, 3]]
    flat, rows = _pack_book_levels(levels)
    assert rows == 2
    assert flat == (10000, 5, 9990, 3)


def test_pack_book_levels_truncates_at_max_levels():
    levels = [[i * 10, i] for i in range(10)]
    flat, rows = _pack_book_levels(levels, max_levels=3)
    assert rows == 3


def test_pack_book_levels_skips_none_rows():
    levels = [None, [10000, 5], None, [9990, 3]]
    flat, rows = _pack_book_levels(levels)
    assert rows == 2


def test_pack_book_levels_returns_none_for_bad_row_elements():
    levels = [["not_a_price", "not_a_vol"]]
    # "not_a_price" can't be cast to int → inner except → returns None
    result = _pack_book_levels(levels)
    assert result is None


def test_pack_book_levels_returns_none_for_non_iterable_row():
    # A row that has no __getitem__ causes exception → returns None
    levels = [42]  # int has no [0] subscript
    result = _pack_book_levels(levels)
    assert result is None


# ---------------------------------------------------------------------------
# _PyFastTickRingBuffer
# ---------------------------------------------------------------------------


def test_py_fast_tick_ring_get_returns_none_on_empty():
    ring = _PyFastTickRingBuffer(8)
    assert ring.get(0) is None


def test_py_fast_tick_ring_set_and_get():
    ring = _PyFastTickRingBuffer(8)
    ring.set_tick(0, "2330", 10000, 1, 10, False, False, 123)
    result = ring.get(0)
    assert result is not None
    assert result[0] == "tick"
    assert result[1] == "2330"
    assert result[2] == 10000


# ---------------------------------------------------------------------------
# _PyFastBidAskRingBuffer
# ---------------------------------------------------------------------------


def test_py_fast_bidask_ring_get_returns_falsy_on_empty():
    ring = _PyFastBidAskRingBuffer(8)
    assert not ring.get(0)


def test_py_fast_bidask_ring_set_bidask_without_stats():
    ring = _PyFastBidAskRingBuffer(8)
    ring.set_bidask(0, "2330", [[10000, 1]], [[10010, 1]], 123, False, False, 0, 0, 0, 0, 0.0, 0.0, 0.0)
    result = ring.get(0)
    assert result is not None
    assert result[0] == "bidask"
    assert result[1] == "2330"


def test_py_fast_bidask_ring_set_bidask_with_stats():
    ring = _PyFastBidAskRingBuffer(8)
    ring.set_bidask(
        idx=0,
        symbol="2330",
        bids=[[10000, 5]],
        asks=[[10010, 3]],
        exch_ts=999,
        is_snapshot=False,
        has_stats=True,
        best_bid=10000,
        best_ask=10010,
        bid_depth=5,
        ask_depth=3,
        mid_price=10005.0,
        spread=10.0,
        imbalance=0.2,
    )
    result = ring.get(0)
    assert result is not None
    assert result[0] == "bidask"
    # With stats the tuple is longer (includes imbalance etc.)
    assert len(result) > 6


def test_py_fast_bidask_ring_packed_roundtrip_no_stats():
    ring = _PyFastBidAskRingBuffer(8)
    ring.set_bidask_packed(
        idx=0,
        symbol="2330",
        bid_flat=(10000, 5),
        bid_rows=1,
        ask_flat=(10010, 3),
        ask_rows=1,
        exch_ts=100,
        is_snapshot=False,
        has_stats=False,
        best_bid=0,
        best_ask=0,
        bid_depth=0,
        ask_depth=0,
        mid_price=0.0,
        spread=0.0,
        imbalance=0.0,
    )
    result = ring.get(0)
    assert result is not None
    assert result[0] == "bidask"
    assert result[1] == "2330"


def test_py_fast_bidask_ring_packed_roundtrip_with_stats():
    ring = _PyFastBidAskRingBuffer(8)
    ring.set_bidask_packed(
        idx=0,
        symbol="2330",
        bid_flat=(10000, 5, 9990, 2),
        bid_rows=2,
        ask_flat=(10010, 3, 10020, 1),
        ask_rows=2,
        exch_ts=100,
        is_snapshot=False,
        has_stats=True,
        best_bid=10000,
        best_ask=10010,
        bid_depth=7,
        ask_depth=4,
        mid_price=10005.0,
        spread=10.0,
        imbalance=0.3,
    )
    result = ring.get(0)
    assert result is not None
    assert result[0] == "bidask"
    assert result[1] == "2330"
    # has_stats=True → extended tuple with best_bid, best_ask, etc.
    assert len(result) >= 13


# ---------------------------------------------------------------------------
# _PyFastLOBStatsRingBuffer
# ---------------------------------------------------------------------------


def test_py_fast_lobstats_ring_get_returns_none_on_empty():
    ring = _PyFastLOBStatsRingBuffer(8)
    assert ring.get(0) is None


def test_py_fast_lobstats_ring_set_and_get():
    ring = _PyFastLOBStatsRingBuffer(8)
    ring.set_stats(0, "2330", 100, 20010, 10, 0.5, 10000, 10010, 5, 3)
    result = ring.get(0)
    assert result is not None
    assert result[0] == "2330"


# ---------------------------------------------------------------------------
# RingBufferBus: set_storm_guard
# ---------------------------------------------------------------------------


def test_set_storm_guard_assigns_reference():
    bus = RingBufferBus(size=4)
    dummy_guard = MagicMock()
    bus.set_storm_guard(dummy_guard)
    assert bus._storm_guard is dummy_guard


# ---------------------------------------------------------------------------
# RingBufferBus: multi-writer publish_many async path
# ---------------------------------------------------------------------------


def test_publish_many_multi_writer_path():
    bus = RingBufferBus(size=8)
    bus.single_writer = False

    async def _run():
        await bus.publish_many(["x", "y", "z"])
        out = []
        async for batch in bus.consume_batch(batch_size=3, start_cursor=-1):
            out.extend(batch)
            return out

    result = asyncio.run(_run())
    assert "x" in result
    assert "y" in result
    assert "z" in result


# ---------------------------------------------------------------------------
# RingBufferBus: pure-Python buffer consume path
# ---------------------------------------------------------------------------


def test_consume_reads_from_python_buffer(monkeypatch):
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", False)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", False)
    bus = RingBufferBus(size=8)
    assert bus.buffer is not None
    assert bus._kind_ring is None

    async def _run():
        bus.publish_nowait("py-event")
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    assert result == "py-event"


def test_consume_batch_reads_from_python_buffer(monkeypatch):
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", False)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", False)
    bus = RingBufferBus(size=8)

    async def _run():
        bus.publish_many_nowait(["a", "b"])
        async for batch in bus.consume_batch(batch_size=2, start_cursor=-1):
            return batch

    result = asyncio.run(_run())
    assert result == ["a", "b"]


# ---------------------------------------------------------------------------
# RingBufferBus: typed tick routing in consume
# ---------------------------------------------------------------------------


def test_consume_typed_tick_ring_routing(monkeypatch):
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", True)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", False)
    bus = RingBufferBus(size=8)
    assert bus._tick_ring is not None
    assert bus._kind_ring is not None

    tick = ("tick", "2330", 10000, 1, 100, False, False, 999)

    async def _run():
        bus.publish_nowait(tick)
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    assert result == tick


# ---------------------------------------------------------------------------
# RingBufferBus: typed book rings — bidask without stats (no rest fields)
# ---------------------------------------------------------------------------


def test_typed_book_ring_bidask_no_stats(monkeypatch):
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", False)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", True)
    bus = RingBufferBus(size=8)

    bidask = ("bidask", "2330", [[10000, 1]], [[10010, 1]], 123, True)

    async def _run():
        bus.publish_nowait(bidask)
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    assert result == bidask


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# RingBufferBus: spin-wait mode (HFT_BUS_WAIT_MODE=spin)
# ---------------------------------------------------------------------------


def test_spin_wait_mode_signal_is_none(monkeypatch):
    monkeypatch.setattr(event_bus_mod, "_WAIT_MODE", "spin")
    bus = RingBufferBus(size=4)
    assert bus.signal is None


def test_spin_wait_consume_event(monkeypatch):
    """consume() works in spin mode (no asyncio.Event)."""
    monkeypatch.setattr(event_bus_mod, "_WAIT_MODE", "spin")

    bus = RingBufferBus(size=4)

    async def _run():
        bus.publish_nowait("spin-event")
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    assert result == "spin-event"


# ---------------------------------------------------------------------------
# RingBufferBus: typed book ring exception fallback → stored in fallback buffer
# ---------------------------------------------------------------------------


def test_typed_book_ring_exception_uses_fallback(monkeypatch):
    """When typed ring set_bidask raises, event must still land in fallback buffer."""
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", False)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", True)
    bus = RingBufferBus(size=8)

    # Patch _bidask_ring.set_bidask_packed to raise so we fall through
    bus._bidask_ring.set_bidask_packed = MagicMock(side_effect=RuntimeError("boom"))
    bus._bidask_ring.set_bidask = MagicMock(side_effect=RuntimeError("boom"))

    bidask = ("bidask", "2330", [[10000, 1]], [[10010, 1]], 123, False)
    bus.publish_nowait(bidask)
    # Event should have been stored in fallback buffer
    assert bus.cursor == 0
    assert bus.buffer is not None
    assert bus.buffer[0] == bidask
