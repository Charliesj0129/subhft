import asyncio

import pytest

from hft_platform.engine import event_bus as event_bus_mod
from hft_platform.engine.event_bus import RingBufferBus


def test_publish_nowait_consume_single():
    bus = RingBufferBus(size=4)

    async def _run():
        bus.publish_nowait("evt-1")
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    assert result == "evt-1"


def test_publish_many_nowait_consume_batch():
    bus = RingBufferBus(size=8)

    async def _run():
        bus.publish_many_nowait(["a", "b", "c"])
        async for batch in bus.consume_batch(batch_size=3, start_cursor=-1):
            return batch

    batch = asyncio.run(_run())
    assert batch == ["a", "b", "c"]


def test_consume_overflow_increments_counter():
    bus = RingBufferBus(size=2)
    bus.publish_many_nowait(["e1", "e2", "e3", "e4", "e5"])

    async def _run():
        async for evt in bus.consume(start_cursor=-1):
            return evt

    asyncio.run(_run())
    assert bus.metrics.bus_overflow_total._value.get() >= 1


def test_publish_multi_writer_path(monkeypatch):
    monkeypatch.setenv("HFT_BUS_SINGLE_WRITER", "0")
    bus = RingBufferBus(size=4)

    async def _run():
        await bus.publish("evt-locked")
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    assert result == "evt-locked"


def test_consume_overflow_triggers_storm_guard(monkeypatch):
    class DummyStormGuard:
        def __init__(self):
            self.halt_messages = []

        def trigger_halt(self, msg: str) -> None:
            self.halt_messages.append(msg)

    monkeypatch.setenv("HFT_BUS_OVERFLOW_HALT_THRESHOLD", "1")
    storm_guard = DummyStormGuard()
    bus = RingBufferBus(size=2, storm_guard=storm_guard)
    bus.publish_many_nowait(["e1", "e2", "e3", "e4", "e5"])

    async def _run():
        async for evt in bus.consume(start_cursor=-1):
            return evt

    asyncio.run(_run())
    assert storm_guard.halt_messages


def test_consume_batch_overflow_triggers_storm_guard(monkeypatch):
    class DummyStormGuard:
        def __init__(self):
            self.halt_messages = []

        def trigger_halt(self, msg: str) -> None:
            self.halt_messages.append(msg)

    monkeypatch.setenv("HFT_BUS_OVERFLOW_HALT_THRESHOLD", "1")
    storm_guard = DummyStormGuard()
    bus = RingBufferBus(size=2, storm_guard=storm_guard)
    bus.publish_many_nowait(["e1", "e2", "e3", "e4", "e5"])

    async def _run():
        async for batch in bus.consume_batch(batch_size=2, start_cursor=-1):
            return batch

    batch = asyncio.run(_run())
    assert batch
    assert storm_guard.halt_messages


def test_typed_tick_ring_feature_flag(monkeypatch):
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", True)
    monkeypatch.setattr(event_bus_mod, "_RUST_TICK_RING_FACTORY", None)
    bus = RingBufferBus(size=8)

    tick = ("tick", "2330", 10000, 5, 50, False, False, 123456789)

    async def _run():
        bus.publish_nowait(tick)
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    assert result == tick


def test_typed_book_rings_feature_flag_bidask_and_lobstats(monkeypatch):
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", False)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", True)
    monkeypatch.setattr(event_bus_mod, "_RUST_BIDASK_RING_FACTORY", None)
    monkeypatch.setattr(event_bus_mod, "_RUST_LOBSTATS_RING_FACTORY", None)
    bus = RingBufferBus(size=8)

    bidask = ("bidask", "2330", [[10000, 1]], [[10010, 1]], 123, False)
    lobstats = ("2330", 123, 20010, 10, 0.1, 10000, 10010, 5, 6)

    async def _run():
        bus.publish_many_nowait([bidask, lobstats])
        out = []
        async for batch in bus.consume_batch(batch_size=2, start_cursor=-1):
            out.extend(batch)
            return out

    result = asyncio.run(_run())
    assert result[0] == bidask
    assert result[1] == lobstats


def test_typed_book_ring_packed_bidask_roundtrip_with_stats(monkeypatch):
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", False)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", True)
    monkeypatch.setattr(event_bus_mod, "_RUST_BIDASK_RING_FACTORY", None)
    monkeypatch.setattr(event_bus_mod, "_RUST_LOBSTATS_RING_FACTORY", None)
    bus = RingBufferBus(size=8)

    bidask = (
        "bidask",
        "2330",
        [[10000, 3], [9990, 2]],
        [[10010, 4], [10020, 5]],
        123,
        False,
        10000,
        10010,
        5,
        9,
        10005.0,
        10.0,
        0.1,
    )

    async def _run():
        bus.publish_nowait(bidask)
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    assert result == bidask
