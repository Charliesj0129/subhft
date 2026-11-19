import asyncio

from hft_platform.engine import event_bus as event_bus_mod
from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.observability.event_bus_telemetry import PrometheusEventBusTelemetry
from hft_platform.observability.metrics import MetricsRegistry


def _bus_with_metrics(size: int) -> tuple[RingBufferBus, MetricsRegistry]:
    registry = MetricsRegistry.get()
    return RingBufferBus(size=size, telemetry=PrometheusEventBusTelemetry(registry)), registry


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
    bus, registry = _bus_with_metrics(size=2)
    initial = registry.bus_overflow_total._value.get()
    bus.publish_many_nowait(["e1", "e2", "e3", "e4", "e5"])

    async def _run():
        async for evt in bus.consume(start_cursor=-1):
            return evt

    asyncio.run(_run())
    assert registry.bus_overflow_total._value.get() == initial + 1


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


def test_overflow_count_resets_after_successful_consume():
    """After a successful consume cycle (no overflow), _overflow_count resets to 0."""
    bus = RingBufferBus(size=2)

    bus.publish_many_nowait(["e1", "e2", "e3", "e4", "e5"])

    async def _run():
        gen = bus.consume(start_cursor=-1)
        gap_evt = await gen.__anext__()
        evt1 = await gen.__anext__()
        evt2 = await gen.__anext__()
        bus.publish_nowait("probe")
        evt3 = await gen.__anext__()
        return (gap_evt, evt1, evt2, evt3)

    result = asyncio.run(_run())
    from hft_platform.events import GapEvent

    assert isinstance(result[0], GapEvent)
    assert result[3] == "probe"
    assert bus._overflow_count == 0


def test_overflow_count_resets_after_successful_consume_batch():
    """consume_batch also resets _overflow_count after successful catch-up."""
    bus = RingBufferBus(size=2)
    bus.publish_many_nowait(["e1", "e2", "e3", "e4", "e5"])

    async def _run():
        gen = bus.consume_batch(batch_size=10, start_cursor=-1)
        gap_batch = await gen.__anext__()
        batch1 = await gen.__anext__()
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

    bus._overflow_count_per_consumer["unknown"] = 2

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
        gap_evt = await gen.__anext__()
        evt1 = await gen.__anext__()
        evt2 = await gen.__anext__()
        bus.publish_nowait("probe")
        evt3 = await gen.__anext__()
        assert evt3 == "probe"
        return bus

    for _ in range(4):
        bus = asyncio.run(_overflow_then_catchup())
        assert bus._overflow_count == 0

    assert len(storm_guard.halt_messages) == 0


def test_consumer_lag_gauge_updates_on_consume():
    """bus_consumer_lag gauge is updated after each catch-up iteration in consume()."""
    bus, registry = _bus_with_metrics(size=16)
    bus.publish_many_nowait(["a", "b", "c"])

    async def _run():
        gen = bus.consume(start_cursor=-1, consumer_name="lag_consumer")
        _e1 = await gen.__anext__()
        _e2 = await gen.__anext__()
        _e3 = await gen.__anext__()
        bus.publish_many_nowait(["d", "e"])
        _e4 = await gen.__anext__()
        _e5 = await gen.__anext__()
        bus.publish_nowait("f")
        _e6 = await gen.__anext__()
        assert "lag_consumer" in bus._consumer_positions
        return _e6

    result = asyncio.run(_run())
    assert result == "f"
    lag_value = registry.bus_consumer_lag.labels(consumer="lag_consumer")._value.get()
    assert lag_value >= 0


def test_consumer_lag_gauge_reflects_writer_distance():
    """Gauge value equals cursor - local_seq at end of catch-up iteration."""
    bus, registry = _bus_with_metrics(size=16)
    bus.publish_many_nowait(["a", "b", "c"])

    async def _run():
        gen = bus.consume(start_cursor=-1, consumer_name="distance_check")
        _e1 = await gen.__anext__()
        _e2 = await gen.__anext__()
        _e3 = await gen.__anext__()
        bus.publish_nowait("d")
        _e4 = await gen.__anext__()
        bus.publish_nowait("e")
        _e5 = await gen.__anext__()
        bus.publish_nowait("sentinel")
        _e6 = await gen.__anext__()
        return _e6

    result = asyncio.run(_run())
    assert result == "sentinel"
    lag_value = registry.bus_consumer_lag.labels(consumer="distance_check")._value.get()
    assert lag_value >= 0


def test_consumer_position_cleaned_up_on_close():
    """Consumer position dict entry is removed when generator is closed."""
    bus = RingBufferBus(size=16)
    bus.publish_nowait("evt")

    async def _run():
        gen = bus.consume(start_cursor=-1, consumer_name="cleanup_test")
        _evt = await gen.__anext__()
        bus.publish_nowait("trigger")
        _evt2 = await gen.__anext__()
        assert "cleanup_test" in bus._consumer_positions
        await gen.aclose()
        assert "cleanup_test" not in bus._consumer_positions

    asyncio.run(_run())


def test_consumer_lag_gauge_with_consume_batch():
    """bus_consumer_lag is also updated when using consume_batch."""
    bus, registry = _bus_with_metrics(size=16)
    bus.publish_many_nowait(["a", "b", "c"])

    async def _run():
        gen = bus.consume_batch(batch_size=10, start_cursor=-1, consumer_name="batch_consumer")
        batch = await gen.__anext__()
        assert batch == ["a", "b", "c"]
        bus.publish_nowait("d")
        batch2 = await gen.__anext__()
        assert batch2 == ["d"]
        assert "batch_consumer" in bus._consumer_positions
        return True

    asyncio.run(_run())
    lag_value = registry.bus_consumer_lag.labels(consumer="batch_consumer")._value.get()
    assert lag_value >= 0


def test_consumer_batch_position_cleaned_up_on_close():
    """Consumer position dict entry is removed when batch generator is closed."""
    bus = RingBufferBus(size=16)
    bus.publish_nowait("evt")

    async def _run():
        gen = bus.consume_batch(batch_size=10, start_cursor=-1, consumer_name="batch_cleanup")
        _batch = await gen.__anext__()
        bus.publish_nowait("trigger")
        _batch2 = await gen.__anext__()
        assert "batch_cleanup" in bus._consumer_positions
        await gen.aclose()
        assert "batch_cleanup" not in bus._consumer_positions

    asyncio.run(_run())
