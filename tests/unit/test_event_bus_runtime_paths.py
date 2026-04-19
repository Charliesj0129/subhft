"""Tests targeting specific runtime paths in engine/event_bus.py for 90%+ coverage.

Focused on:
- Lines 460-462: tick typed ring set_tick exception → fallback buffer
- Line 507: bidask ring without set_bidask_packed → direct set_bidask fallback path
- Lines 536-555: lobstats typed ring write (happy path) and exception → fallback
- Line 599: async publish() multi-writer with signal.set() branch
- Lines 604-607: async publish_many() single_writer=True path
"""

import asyncio
from unittest.mock import MagicMock

from hft_platform.engine import event_bus as event_bus_mod
from hft_platform.engine.event_bus import RingBufferBus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_py_bus(monkeypatch, *, use_typed_tick=False, use_typed_book=False, size=8):
    """Create a pure-Python RingBufferBus with optional typed ring flags."""
    monkeypatch.setattr(event_bus_mod, "_RUST_ENABLED", False)
    monkeypatch.setattr(event_bus_mod, "_USE_RUST_BUS", False)
    monkeypatch.setattr(event_bus_mod, "_RUST_RING_FACTORY", None)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", use_typed_tick)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", use_typed_book)
    monkeypatch.setattr(event_bus_mod, "_RUST_TICK_RING_FACTORY", None)
    monkeypatch.setattr(event_bus_mod, "_RUST_BIDASK_RING_FACTORY", None)
    monkeypatch.setattr(event_bus_mod, "_RUST_LOBSTATS_RING_FACTORY", None)
    return RingBufferBus(size=size)


# ---------------------------------------------------------------------------
# Lines 460-462: tick typed ring set_tick raises → handled_by_typed_ring=False
# → event stored in fallback buffer
# ---------------------------------------------------------------------------


def test_tick_ring_set_tick_exception_falls_back_to_buffer(monkeypatch):
    """When tick ring set_tick raises, event must be stored in fallback buffer."""
    bus = _make_py_bus(monkeypatch, use_typed_tick=True)
    assert bus._tick_ring is not None
    assert bus._kind_ring is not None

    # Patch set_tick to raise so lines 460-462 are executed
    bus._tick_ring.set_tick = MagicMock(side_effect=ValueError("bad tick data"))

    tick = ("tick", "2330", 10000, 1, 100, False, False, 999)
    bus.publish_nowait(tick)

    # cursor advanced
    assert bus.cursor == 0
    # kind_ring slot must be 0 (fallback, not typed)
    assert bus._kind_ring[0] == 0
    # event must be in fallback buffer
    assert bus.buffer is not None
    assert bus.buffer[0] == tick


def test_tick_ring_set_tick_exception_and_consume_returns_event(monkeypatch):
    """After tick ring exception, consumer still reads the event from fallback buffer."""
    bus = _make_py_bus(monkeypatch, use_typed_tick=True)
    bus._tick_ring.set_tick = MagicMock(side_effect=RuntimeError("ring full"))

    tick = ("tick", "2330", 12345, 2, 50, True, False, 111222333)

    async def _run():
        bus.publish_nowait(tick)
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    assert result == tick


# ---------------------------------------------------------------------------
# Line 507: bidask ring has no set_bidask_packed attribute
# → used_packed stays False → direct set_bidask is called
# ---------------------------------------------------------------------------


def test_bidask_ring_without_packed_writer_uses_set_bidask(monkeypatch):
    """When set_bidask_packed is absent, fall through to set_bidask (line 507)."""
    bus = _make_py_bus(monkeypatch, use_typed_book=True)
    assert bus._bidask_ring is not None

    # Replace with a mock object that has set_bidask but NOT set_bidask_packed,
    # so getattr returns None and callable() is False → line 507 executes.
    set_bidask_mock = MagicMock()
    mock_ring = MagicMock(spec=["set_bidask", "get"])
    mock_ring.set_bidask = set_bidask_mock
    bus._bidask_ring = mock_ring

    bidask = ("bidask", "2330", [[10000, 1]], [[10010, 1]], 123, False)
    bus.publish_nowait(bidask)

    # set_bidask must have been called (not packed path)
    set_bidask_mock.assert_called_once()
    assert bus.cursor == 0


def test_bidask_ring_without_packed_writer_roundtrip(monkeypatch):
    """Consumer reads back correct event when set_bidask_packed is absent."""
    bus = _make_py_bus(monkeypatch, use_typed_book=True)

    # Replace bidask ring with minimal object that stores/returns via set_bidask
    from hft_platform.engine.event_bus import _PyFastBidAskRingBuffer

    class _NoPacked(_PyFastBidAskRingBuffer):
        """BidAsk ring without the packed writer method."""

    # Verify the subclass doesn't accidentally inherit set_bidask_packed
    # (it does inherit it, so we must shadow it with None/non-callable)
    ring = _NoPacked(8)
    ring.set_bidask_packed = None  # override to non-callable
    bus._bidask_ring = ring

    bidask = ("bidask", "2330", [[10000, 1]], [[10010, 1]], 456, True)

    async def _run():
        bus.publish_nowait(bidask)
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    assert result == bidask


def test_bidask_ring_without_packed_writer_with_stats(monkeypatch):
    """Full-stats bidask via set_bidask (no packed writer) stores and retrieves correctly."""
    bus = _make_py_bus(monkeypatch, use_typed_book=True)

    from hft_platform.engine.event_bus import _PyFastBidAskRingBuffer

    ring = _PyFastBidAskRingBuffer(8)
    ring.set_bidask_packed = None  # make it non-callable → falls through to set_bidask
    bus._bidask_ring = ring

    bidask = (
        "bidask",
        "2330",
        [[10000, 5], [9990, 2]],
        [[10010, 4], [10020, 1]],
        789,
        False,
        10000,
        10010,
        7,
        5,
        10005.0,
        10.0,
        0.25,
    )

    async def _run():
        bus.publish_nowait(bidask)
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    assert result is not None
    assert result[0] == "bidask"
    assert result[1] == "2330"


# ---------------------------------------------------------------------------
# Lines 536-552: lobstats typed ring happy path
# (set_stats called, kind_ring[seq]=3, handled_by_typed_ring=True)
# ---------------------------------------------------------------------------


def test_lobstats_typed_ring_write_sets_kind_3(monkeypatch):
    """Publishing a 10-element lobstats tuple writes via lobstats ring (kind=3)."""
    bus = _make_py_bus(monkeypatch, use_typed_book=True)
    assert bus._lobstats_ring is not None
    assert bus._kind_ring is not None

    # 10-element lobstats tuple: (tag_str, symbol, ts, mid_x2, spread_scaled,
    #   imbalance, best_bid, best_ask, bid_depth, ask_depth)
    # Unpacked as: _tag=event[0], symbol=event[1], ts=event[2], ...
    lobstats = ("lobstats", "2330", 100, 20010, 10, 0.5, 10000, 10010, 5, 3)

    bus.publish_nowait(lobstats)

    # kind_ring must be 3 for lobstats
    assert bus._kind_ring[0] == 3
    # cursor advanced
    assert bus.cursor == 0


def test_lobstats_typed_ring_roundtrip_via_consume(monkeypatch):
    """Consumer reads lobstats event back (prepended with 'lobstats' tag)."""
    bus = _make_py_bus(monkeypatch, use_typed_book=True)

    # Tuple: (tag, symbol, ts, mid_x2, spread_scaled, imbalance, best_bid,
    #          best_ask, bid_depth, ask_depth) — 10 elements, event[0] is str
    lobstats = ("lobstats", "2330", 200, 20020, 8, 0.3, 10001, 10009, 4, 6)

    async def _run():
        bus.publish_nowait(lobstats)
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    # consume() returns ("lobstats",) + raw where raw = set_stats stored 9-tuple
    # raw = (str(symbol), int(ts), int(mid_x2), int(spread_scaled), float(imbalance),
    #         int(best_bid), int(best_ask), int(bid_depth), int(ask_depth))
    assert result is not None
    assert result[0] == "lobstats"
    # symbol was stored as str("2330") → result[1]
    assert result[1] == "2330"


def test_lobstats_typed_ring_set_stats_called(monkeypatch):
    """Verify set_stats is invoked with correct arguments (lines 538-549)."""
    bus = _make_py_bus(monkeypatch, use_typed_book=True)
    set_stats_mock = MagicMock(wraps=bus._lobstats_ring.set_stats)
    bus._lobstats_ring.set_stats = set_stats_mock

    # (tag, symbol, ts, mid_x2, spread_scaled, imbalance, best_bid, best_ask,
    #  bid_depth, ask_depth)
    lobstats = ("lobstats", "2330", 300, 20030, 12, 0.7, 10002, 10014, 6, 8)
    bus.publish_nowait(lobstats)

    set_stats_mock.assert_called_once()
    call_args = set_stats_mock.call_args[0]
    # set_stats signature: (idx, symbol, ts, mid_x2, spread_scaled, imbalance, ...)
    # idx = next_seq (0), symbol = event[1] = "2330", ts = event[2] = 300
    assert call_args[0] == 0  # idx = next_seq
    assert call_args[1] == "2330"  # symbol
    assert call_args[2] == 300  # ts
    assert call_args[3] == 20030  # mid_x2


# ---------------------------------------------------------------------------
# Lines 553-555: lobstats ring set_stats raises → fallback buffer
# ---------------------------------------------------------------------------


def test_lobstats_ring_set_stats_exception_falls_back_to_buffer(monkeypatch):
    """When lobstats ring set_stats raises, event falls back to generic buffer."""
    bus = _make_py_bus(monkeypatch, use_typed_book=True)
    bus._lobstats_ring.set_stats = MagicMock(side_effect=TypeError("bad stats"))

    lobstats = ("lobstats", "2330", 400, 20040, 15, 0.6, 10003, 10015, 7, 9)
    bus.publish_nowait(lobstats)

    # handled_by_typed_ring must have been set to False → fallback
    assert bus.cursor == 0
    # kind_ring slot must be 0 (fallback)
    assert bus._kind_ring is not None
    assert bus._kind_ring[0] == 0
    # event in fallback buffer
    assert bus.buffer is not None
    assert bus.buffer[0] == lobstats


def test_lobstats_ring_exception_consume_returns_event(monkeypatch):
    """After lobstats ring exception, consumer reads raw event from fallback."""
    bus = _make_py_bus(monkeypatch, use_typed_book=True)
    bus._lobstats_ring.set_stats = MagicMock(side_effect=RuntimeError("overflow"))

    lobstats = ("lobstats", "2330", 500, 20050, 20, 0.1, 10004, 10016, 3, 2)

    async def _run():
        bus.publish_nowait(lobstats)
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    # Falls back to Python buffer → returns original tuple unchanged
    assert result == lobstats


# ---------------------------------------------------------------------------
# Line 599: async publish() multi-writer path — signal.set() branch
# (signal is not None, so lines 599-600 execute)
# ---------------------------------------------------------------------------


def test_async_publish_multi_writer_sets_signal(monkeypatch):
    """async publish() with single_writer=False and signal != None sets the signal."""
    monkeypatch.setattr(event_bus_mod, "_WAIT_MODE", "event")  # signal is not None
    monkeypatch.setattr(event_bus_mod, "_RUST_ENABLED", False)
    monkeypatch.setattr(event_bus_mod, "_USE_RUST_BUS", False)
    monkeypatch.setattr(event_bus_mod, "_RUST_RING_FACTORY", None)

    bus = RingBufferBus(size=8)
    bus.single_writer = False
    assert bus.signal is not None  # event mode

    async def _run():
        await bus.publish("multi-writer-event")
        return bus.cursor

    result = asyncio.run(_run())
    assert result == 0
    # signal was set inside write_lock branch (line 599-600)
    assert bus.signal.is_set()


def test_async_publish_multi_writer_consumer_reads_event(monkeypatch):
    """Consumer receives event published via multi-writer async publish() path."""
    monkeypatch.setattr(event_bus_mod, "_WAIT_MODE", "event")
    monkeypatch.setattr(event_bus_mod, "_RUST_ENABLED", False)
    monkeypatch.setattr(event_bus_mod, "_USE_RUST_BUS", False)
    monkeypatch.setattr(event_bus_mod, "_RUST_RING_FACTORY", None)

    bus = RingBufferBus(size=8)
    bus.single_writer = False

    async def _run():
        await bus.publish("locked-event")
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    assert result == "locked-event"


# ---------------------------------------------------------------------------
# Lines 604-607: async publish_many() single_writer=True path
# ---------------------------------------------------------------------------


def test_async_publish_many_single_writer_path(monkeypatch):
    """async publish_many() with default single_writer=True uses fast path (604-607)."""
    monkeypatch.setattr(event_bus_mod, "_RUST_ENABLED", False)
    monkeypatch.setattr(event_bus_mod, "_USE_RUST_BUS", False)
    monkeypatch.setattr(event_bus_mod, "_RUST_RING_FACTORY", None)

    bus = RingBufferBus(size=8)
    assert bus.single_writer is True  # default

    async def _run():
        await bus.publish_many(["a", "b", "c"])
        out = []
        async for batch in bus.consume_batch(batch_size=3, start_cursor=-1):
            out.extend(batch)
            return out

    result = asyncio.run(_run())
    assert result == ["a", "b", "c"]


def test_async_publish_many_single_writer_calls_notify(monkeypatch):
    """async publish_many() single_writer path calls _notify (signal.set)."""
    monkeypatch.setattr(event_bus_mod, "_WAIT_MODE", "event")
    monkeypatch.setattr(event_bus_mod, "_RUST_ENABLED", False)
    monkeypatch.setattr(event_bus_mod, "_USE_RUST_BUS", False)
    monkeypatch.setattr(event_bus_mod, "_RUST_RING_FACTORY", None)

    bus = RingBufferBus(size=8)
    assert bus.single_writer is True
    assert bus.signal is not None

    async def _run():
        await bus.publish_many(["x", "y"])

    asyncio.run(_run())
    # _notify() must have been called → signal set
    assert bus.signal.is_set()
    assert bus.cursor == 1  # 0-indexed, two events → cursor at 1


def test_async_publish_many_single_writer_preserves_order(monkeypatch):
    """Events published via single_writer async publish_many() arrive in order."""
    monkeypatch.setattr(event_bus_mod, "_RUST_ENABLED", False)
    monkeypatch.setattr(event_bus_mod, "_USE_RUST_BUS", False)
    monkeypatch.setattr(event_bus_mod, "_RUST_RING_FACTORY", None)

    bus = RingBufferBus(size=16)

    async def _run():
        await bus.publish_many([10, 20, 30, 40, 50])
        collected = []
        async for evt in bus.consume(start_cursor=-1):
            collected.append(evt)
            if len(collected) == 5:
                return collected

    result = asyncio.run(_run())
    assert result == [10, 20, 30, 40, 50]


# ---------------------------------------------------------------------------
# Combined: tick exception + lobstats exception in single publish sequence
# ---------------------------------------------------------------------------


def test_mixed_ring_exceptions_all_fall_back_to_buffer(monkeypatch):
    """Multiple typed rings failing simultaneously → all events in fallback buffer."""
    bus = _make_py_bus(monkeypatch, use_typed_tick=True, use_typed_book=True)

    bus._tick_ring.set_tick = MagicMock(side_effect=ValueError("tick boom"))
    bus._lobstats_ring.set_stats = MagicMock(side_effect=ValueError("stats boom"))
    bus._bidask_ring.set_bidask_packed = MagicMock(side_effect=ValueError("bidask boom"))
    bus._bidask_ring.set_bidask = MagicMock(side_effect=ValueError("bidask boom 2"))

    tick = ("tick", "2330", 10000, 1, 100, False, False, 111)
    lobstats = ("lobstats", "2330", 200, 20010, 5, 0.2, 10000, 10010, 3, 4)
    bidask = ("bidask", "2330", [[10000, 1]], [[10010, 1]], 300, False)

    bus.publish_many_nowait([tick, lobstats, bidask])

    assert bus.cursor == 2
    assert bus.buffer is not None
    # All three events end up in fallback buffer at positions 0, 1, 2
    assert bus.buffer[0] == tick
    assert bus.buffer[1] == lobstats
    assert bus.buffer[2] == bidask


# ---------------------------------------------------------------------------
# async publish() multi-writer: signal=None branch (spin mode, line 599 NOT taken)
# covers the negative — signal is None, so only _publish_unlocked is called
# ---------------------------------------------------------------------------


def test_async_publish_multi_writer_spin_mode_no_signal(monkeypatch):
    """async publish() multi-writer with spin mode: signal is None, line 599 skipped."""
    monkeypatch.setattr(event_bus_mod, "_WAIT_MODE", "spin")
    monkeypatch.setattr(event_bus_mod, "_RUST_ENABLED", False)
    monkeypatch.setattr(event_bus_mod, "_USE_RUST_BUS", False)
    monkeypatch.setattr(event_bus_mod, "_RUST_RING_FACTORY", None)

    bus = RingBufferBus(size=8)
    bus.single_writer = False
    assert bus.signal is None  # spin mode: no event

    async def _run():
        await bus.publish("spin-multi-event")
        return bus.cursor

    result = asyncio.run(_run())
    # Event was published (cursor advanced)
    assert result == 0
    assert bus.buffer is not None
    assert bus.buffer[0] == "spin-multi-event"
