import asyncio

import pytest

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
