import asyncio

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


# ---------------------------------------------------------------------------
# Overflow counter windowed-reset tests
# ---------------------------------------------------------------------------


def test_overflow_count_resets_after_successful_consume():
    """After a successful consume cycle (no overflow), _overflow_count resets to 0."""
    bus = RingBufferBus(size=2)

    # Publish 5 events into size-2 buffer → overflow when consuming from start.
    bus.publish_many_nowait(["e1", "e2", "e3", "e4", "e5"])

    async def _run():
        gen = bus.consume(start_cursor=-1)
        # Drain all available events from the inner while loop so it completes,
        # which triggers the reset code. With size=2 and 5 published, after
        # overflow skip local_seq = cursor - size = 5 - 2 = 3. Inner loop
        # yields events at seq 4 and 5 (2 events).
        evt1 = await gen.__anext__()
        evt2 = await gen.__anext__()
        # After evt2, the inner while loop condition (local_seq < current_cursor)
        # is False, so the reset runs, then outer loop waits for new data.
        # Publish one more to unblock the wait so we can check the counter.
        bus.publish_nowait("probe")
        evt3 = await gen.__anext__()
        return (evt1, evt2, evt3)

    result = asyncio.run(_run())
    assert result[2] == "probe"
    # Counter was reset after the first catch-up completed.
    assert bus._overflow_count == 0


def test_overflow_count_resets_after_successful_consume_batch():
    """consume_batch also resets _overflow_count after successful catch-up."""
    bus = RingBufferBus(size=2)
    bus.publish_many_nowait(["e1", "e2", "e3", "e4", "e5"])

    async def _run():
        gen = bus.consume_batch(batch_size=10, start_cursor=-1)
        # First batch drains all available events (inner loop completes → reset).
        batch1 = await gen.__anext__()
        # Publish a probe to unblock the outer wait loop.
        bus.publish_nowait("probe")
        batch2 = await gen.__anext__()
        return (batch1, batch2)

    result = asyncio.run(_run())
    assert "probe" in result[1]
    assert bus._overflow_count == 0


def test_consecutive_overflows_trigger_halt(monkeypatch):
    """3 consecutive overflows (no successful catch-up between them) trigger HALT."""

    class DummyStormGuard:
        def __init__(self):
            self.halt_messages = []

        def trigger_halt(self, msg: str) -> None:
            self.halt_messages.append(msg)

    monkeypatch.setenv("HFT_BUS_OVERFLOW_HALT_THRESHOLD", "3")
    storm_guard = DummyStormGuard()
    bus = RingBufferBus(size=2, storm_guard=storm_guard)

    # Each call: publish enough to overflow, consume one event, break.
    # We monkey-patch the reset away to simulate 3 consecutive overflows
    # without a successful full catch-up resetting the counter.
    # Actually: just set _overflow_count directly to simulate accumulated overflows.
    bus._overflow_count = 2  # already had 2 overflows

    # Now cause one more overflow (3rd)
    bus.publish_many_nowait(["e1", "e2", "e3", "e4", "e5"])

    async def _run():
        async for evt in bus.consume(start_cursor=-1):
            return evt

    asyncio.run(_run())
    assert len(storm_guard.halt_messages) == 1
    assert "overflow" in storm_guard.halt_messages[0].lower()


def test_non_consecutive_overflows_do_not_trigger_halt(monkeypatch):
    """Overflows separated by successful consumes do NOT accumulate toward HALT."""

    class DummyStormGuard:
        def __init__(self):
            self.halt_messages = []

        def trigger_halt(self, msg: str) -> None:
            self.halt_messages.append(msg)

    monkeypatch.setenv("HFT_BUS_OVERFLOW_HALT_THRESHOLD", "3")
    storm_guard = DummyStormGuard()

    async def _overflow_then_catchup():
        """Create a fresh bus, overflow it, fully drain → reset counter."""
        bus = RingBufferBus(size=2, storm_guard=storm_guard)
        bus.publish_many_nowait(["x1", "x2", "x3", "x4", "x5"])
        gen = bus.consume(start_cursor=-1)
        # Drain all available events so the inner while loop completes.
        evt1 = await gen.__anext__()
        evt2 = await gen.__anext__()
        # Inner loop done → reset fires. Publish probe to verify.
        bus.publish_nowait("probe")
        evt3 = await gen.__anext__()
        assert evt3 == "probe"
        return bus

    # Do 4 overflow cycles, each separated by a successful catch-up.
    for _ in range(4):
        bus = asyncio.run(_overflow_then_catchup())
        assert bus._overflow_count == 0

    # No HALT should have been triggered despite 4 total overflows.
    assert len(storm_guard.halt_messages) == 0
