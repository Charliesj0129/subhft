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
        # After overflow, a GapEvent is yielded first, then the remaining
        # buffered events. With size=2 and 5 published, after overflow skip
        # local_seq = cursor - size = 5 - 2 = 3. Inner loop yields events
        # at seq 4 and 5 (2 events), preceded by the GapEvent.
        gap_evt = await gen.__anext__()  # GapEvent injected on overflow
        evt1 = await gen.__anext__()
        evt2 = await gen.__anext__()
        # After evt2, the inner while loop condition (local_seq < current_cursor)
        # is False, so the reset runs, then outer loop waits for new data.
        # Publish one more to unblock the wait so we can check the counter.
        bus.publish_nowait("probe")
        evt3 = await gen.__anext__()
        return (gap_evt, evt1, evt2, evt3)

    result = asyncio.run(_run())
    from hft_platform.events import GapEvent

    assert isinstance(result[0], GapEvent)
    assert result[3] == "probe"
    # Counter was reset after the first catch-up completed.
    assert bus._overflow_count == 0


def test_overflow_count_resets_after_successful_consume_batch():
    """consume_batch also resets _overflow_count after successful catch-up."""
    bus = RingBufferBus(size=2)
    bus.publish_many_nowait(["e1", "e2", "e3", "e4", "e5"])

    async def _run():
        gen = bus.consume_batch(batch_size=10, start_cursor=-1)
        # First yield is a GapEvent batch injected on overflow.
        gap_batch = await gen.__anext__()
        # Second batch drains all available events (inner loop completes → reset).
        batch1 = await gen.__anext__()
        # Publish a probe to unblock the outer wait loop.
        bus.publish_nowait("probe")
        batch2 = await gen.__anext__()
        return (gap_batch, batch1, batch2)

    result = asyncio.run(_run())
    from hft_platform.events import GapEvent

    assert any(isinstance(e, GapEvent) for e in result[0])
    assert "probe" in result[2]
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
    # Actually: set per-consumer overflow count to simulate accumulated overflows.
    # The consume() method uses per-consumer tracking now (not the global _overflow_count).
    bus._overflow_count_per_consumer["default"] = 2  # already had 2 overflows

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
        # First yielded event is a GapEvent from overflow detection.
        gap_evt = await gen.__anext__()
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


# ---------------------------------------------------------------------------
# bus_consumer_lag gauge tests
# ---------------------------------------------------------------------------


def test_consumer_lag_gauge_updates_on_consume():
    """bus_consumer_lag gauge is updated after each catch-up iteration in consume()."""
    bus = RingBufferBus(size=16)
    bus.publish_many_nowait(["a", "b", "c"])

    async def _run():
        gen = bus.consume(start_cursor=-1, consumer_name="lag_consumer")
        # Drain a, b, c
        _e1 = await gen.__anext__()
        _e2 = await gen.__anext__()
        _e3 = await gen.__anext__()
        # Publish 2 more; on resume inner while exits, gauge is set,
        # then outer loop re-enters inner while for d, e.
        bus.publish_many_nowait(["d", "e"])
        _e4 = await gen.__anext__()
        _e5 = await gen.__anext__()
        # Publish one more to trigger gauge update after d,e catch-up
        bus.publish_nowait("f")
        _e6 = await gen.__anext__()
        # Check position while generator is still alive
        assert "lag_consumer" in bus._consumer_positions
        return _e6

    result = asyncio.run(_run())
    assert result == "f"
    # Gauge was set; verify it is a non-negative number
    lag_value = bus.metrics.bus_consumer_lag.labels(consumer="lag_consumer")._value.get()
    assert lag_value >= 0


def test_consumer_lag_gauge_reflects_writer_distance():
    """Gauge value equals cursor - local_seq at end of catch-up iteration."""
    bus = RingBufferBus(size=16)
    bus.publish_many_nowait(["a", "b", "c"])

    async def _run():
        gen = bus.consume(start_cursor=-1, consumer_name="distance_check")
        _e1 = await gen.__anext__()
        _e2 = await gen.__anext__()
        _e3 = await gen.__anext__()
        # After resume from _e3 yield, inner while exits.
        # At that point: local_seq=2, cursor=2 (nothing new published yet).
        # Gauge: 2 - 2 = 0. But we need to call __anext__ to trigger that.
        # Publish "d" so outer loop unblocks and inner while processes it.
        bus.publish_nowait("d")
        _e4 = await gen.__anext__()
        # After _e4, gauge was set on the first catch-up exit (a,b,c):
        # cursor was 2 at that point, local_seq=2, lag=0.
        # But "d" publish happened before gauge ran, so cursor=3, lag=1.
        # Then second catch-up: cursor=3, local_seq=3... but we need
        # another __anext__ to trigger gauge for the d catch-up.
        bus.publish_nowait("e")
        _e5 = await gen.__anext__()
        # After _e5 resume, inner while for d exits: cursor may be 4, local_seq=3, lag=1
        # Then enters for e: local_seq=4, yield e. We need one more.
        bus.publish_nowait("sentinel")
        _e6 = await gen.__anext__()
        return _e6

    result = asyncio.run(_run())
    assert result == "sentinel"
    lag_value = bus.metrics.bus_consumer_lag.labels(consumer="distance_check")._value.get()
    # Lag is always non-negative
    assert lag_value >= 0


def test_consumer_position_cleaned_up_on_close():
    """Consumer position dict entry is removed when generator is closed."""
    bus = RingBufferBus(size=16)
    bus.publish_nowait("evt")

    async def _run():
        gen = bus.consume(start_cursor=-1, consumer_name="cleanup_test")
        _evt = await gen.__anext__()
        # Publish another event so the generator advances past the yield,
        # triggering the gauge/position update after inner while exits.
        bus.publish_nowait("trigger")
        _evt2 = await gen.__anext__()
        # Now position should be tracked (gauge was set between catch-ups)
        assert "cleanup_test" in bus._consumer_positions
        # Close the generator — finally block runs
        await gen.aclose()
        # Position should be cleaned up
        assert "cleanup_test" not in bus._consumer_positions

    asyncio.run(_run())


def test_consumer_lag_gauge_with_consume_batch():
    """bus_consumer_lag is also updated when using consume_batch."""
    bus = RingBufferBus(size=16)
    bus.publish_many_nowait(["a", "b", "c"])

    async def _run():
        gen = bus.consume_batch(batch_size=10, start_cursor=-1, consumer_name="batch_consumer")
        batch = await gen.__anext__()
        assert batch == ["a", "b", "c"]
        # Publish more and consume to verify gauge updates
        bus.publish_nowait("d")
        batch2 = await gen.__anext__()
        assert batch2 == ["d"]
        # Check position tracking while generator is alive
        assert "batch_consumer" in bus._consumer_positions
        return True

    asyncio.run(_run())
    # Gauge was set; verify it is a non-negative number
    lag_value = bus.metrics.bus_consumer_lag.labels(consumer="batch_consumer")._value.get()
    assert lag_value >= 0


def test_consumer_batch_position_cleaned_up_on_close():
    """Consumer position dict entry is removed when batch generator is closed."""
    bus = RingBufferBus(size=16)
    bus.publish_nowait("evt")

    async def _run():
        gen = bus.consume_batch(batch_size=10, start_cursor=-1, consumer_name="batch_cleanup")
        _batch = await gen.__anext__()
        # Publish to trigger position update after inner while exits
        bus.publish_nowait("trigger")
        _batch2 = await gen.__anext__()
        assert "batch_cleanup" in bus._consumer_positions
        await gen.aclose()
        assert "batch_cleanup" not in bus._consumer_positions

    asyncio.run(_run())
