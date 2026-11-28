"""Consumed-slot release in RingBufferBus: slots every registered consumer has read are set to None.

The ring otherwise pins the last `size` events (and every GC-tracked object they own) in the old GC
generations long after all consumers read them. These tests drive real consume()/consume_batch()
generators; none of them writes the consumer position map.
"""

import asyncio
import contextlib
import gc
import weakref
from types import SimpleNamespace
from typing import Any

import pytest

from hft_platform.engine import event_bus as event_bus_mod
from hft_platform.events import GapEvent
from hft_platform.strategy.runner import StrategyRunner

_BOTH_READ_PATHS = pytest.mark.parametrize("batch_size", [0, 4], ids=["consume", "consume_batch"])


class _Ev:
    """Weak-referenceable stand-in for a published event; seq is its ring sequence number."""

    __slots__ = ("__weakref__", "seq")

    def __init__(self, seq: int) -> None:
        self.seq = seq

    def __repr__(self) -> str:
        return f"_Ev({self.seq})"


class _Counter:
    def __init__(self) -> None:
        self.count = 0

    def inc(self) -> None:
        self.count += 1


class _LagSink:
    def set(self, lag: int) -> None:
        return None


class _Telemetry:
    def __init__(self) -> None:
        self.overflow_counter = _Counter()
        self.gap_event_counter = _Counter()

    def bind_consumer(self, consumer: str) -> _LagSink:
        return _LagSink()


class _Guard:
    """Stand-in StormGuard that records every HALT request."""

    def __init__(self) -> None:
        self.halts: list[str] = []

    def trigger_halt(self, message: str) -> None:
        self.halts.append(message)


class _RecordingLogger:
    """Stand-in for the module logger: records (level, event, fields) for every call."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def __getattr__(self, level: str):
        def record(event: str, **fields: Any) -> None:
            self.records.append((level, event, fields))

        return record

    def events(self, name: str) -> list[tuple[str, str, dict[str, Any]]]:
        return [record for record in self.records if record[1] == name]


class _Consumer:
    """A real consume()/consume_batch() generator run as a task, recording what it yields in order."""

    def __init__(
        self,
        bus: event_bus_mod.RingBufferBus,
        name: str,
        *,
        start_cursor: int | None = None,
        batch_size: int = 0,
        gate_after: int | None = None,
    ) -> None:
        self.items: list[Any] = []
        self.gate = asyncio.Event()
        self._bus = bus
        self._name = name
        self._start_cursor = start_cursor
        self._batch_size = batch_size
        self._gate_after = gate_after
        self._task: asyncio.Future[None] | None = None

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        if self._batch_size:
            source = self._bus.consume_batch(
                self._batch_size, start_cursor=self._start_cursor, consumer_name=self._name
            )
        else:
            source = self._bus.consume(start_cursor=self._start_cursor, consumer_name=self._name)
        async for item in source:
            for event in item if self._batch_size else (item,):
                self.items.append(event)
                if self._gate_after is not None and len(self.items) == self._gate_after:
                    await self.gate.wait()

    async def stop(self) -> None:
        assert self._task is not None
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


async def _settle() -> None:
    """Let every runnable consumer task run until it is parked again."""
    for _ in range(10):
        await asyncio.sleep(0)


def _make_bus(
    monkeypatch: pytest.MonkeyPatch,
    *,
    size: int = 16,
    release_every: str = "1",
    release: str = "1",
    single_writer: str = "1",
    telemetry: _Telemetry | None = None,
    storm_guard: _Guard | None = None,
    halt_threshold: str = "3",
) -> event_bus_mod.RingBufferBus:
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", False)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", False)
    monkeypatch.setattr(event_bus_mod, "_WAIT_MODE", "event")
    monkeypatch.setenv("HFT_BUS_RELEASE_EVERY", release_every)
    monkeypatch.setenv("HFT_BUS_RELEASE_CONSUMED", release)
    monkeypatch.setenv("HFT_BUS_SINGLE_WRITER", single_writer)
    monkeypatch.setenv("HFT_BUS_OVERFLOW_HALT_THRESHOLD", halt_threshold)
    return event_bus_mod.RingBufferBus(size=size, telemetry=telemetry, storm_guard=storm_guard)


def _publish(bus: event_bus_mod.RingBufferBus, first_seq: int, count: int) -> list[_Ev]:
    events = [_Ev(seq) for seq in range(first_seq, first_seq + count)]
    for event in events:
        bus.publish_nowait(event)
    return events


def _accounted_seqs(items: list[Any]) -> list[int]:
    """Every seq a consumer was told about, in order: a yielded event is its own seq, a GapEvent its range."""
    seqs: list[int] = []
    for item in items:
        if isinstance(item, GapEvent):
            seqs.extend(range(item.first_missed_seq, item.last_missed_seq + 1))
        else:
            seqs.append(item.seq)
    return seqs


@pytest.fixture
def gc_off():
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


@_BOTH_READ_PATHS
def test_release_drops_consumed_slots_and_keeps_unread_ones(monkeypatch, batch_size):
    bus = _make_bus(monkeypatch, size=16)

    async def scenario():
        consumer = _Consumer(bus, "only", start_cursor=-1, batch_size=batch_size)
        consumer.start()
        await _settle()
        events = _publish(bus, 0, 5)
        await _settle()
        events += _publish(bus, 5, 1)  # the consumer's position is 4 when seq 5 is published
        released_upto = bus.released_upto
        slots = list(bus.buffer[:6])
        await consumer.stop()
        return events, released_upto, slots, consumer.items

    events, released_upto, slots, items = asyncio.run(scenario())

    assert released_upto == 4
    assert slots == [None, None, None, None, None, events[5]]
    assert items == events[:5]


def test_release_skips_everything_when_no_consumer_is_registered(monkeypatch):
    bus = _make_bus(monkeypatch, size=16)

    events = _publish(bus, 0, 40)

    assert bus.released_upto == -1
    assert [bus.buffer[seq % 16] for seq in range(24, 40)] == events[24:]


def test_release_holds_at_a_closed_consumers_pin_instead_of_running_past_it(monkeypatch):
    bus = _make_bus(monkeypatch, size=32)

    async def scenario():
        consumer = _Consumer(bus, "only", start_cursor=-1)
        consumer.start()
        await _settle()
        _publish(bus, 0, 5)
        await _settle()
        events = _publish(bus, 5, 1)
        await consumer.stop()
        events += _publish(bus, 6, 20)
        return events

    events = asyncio.run(scenario())

    assert "only" not in bus._consumer_positions
    assert bus.released_upto == 4
    assert [bus.buffer[seq] for seq in range(5, 26)] == events


@pytest.mark.parametrize("flag", ["0", "false", "no", "off"])
def test_release_disabled_by_env_keeps_every_consumed_slot(monkeypatch, flag):
    bus = _make_bus(monkeypatch, size=16, release=flag)

    async def scenario():
        consumer = _Consumer(bus, "only", start_cursor=-1)
        consumer.start()
        await _settle()
        events = _publish(bus, 0, 5)
        await _settle()
        events += _publish(bus, 5, 3)
        await _settle()
        await consumer.stop()
        return events, consumer.items

    events, items = asyncio.run(scenario())

    assert bus.released_upto == -1
    assert bus.buffer[:8] == events
    assert items == events


@_BOTH_READ_PATHS
def test_release_waits_for_the_slowest_consumer_and_loses_nothing(monkeypatch, batch_size):
    bus = _make_bus(monkeypatch, size=64, release_every="4")

    async def scenario():
        fast = _Consumer(bus, "fast", start_cursor=-1, batch_size=batch_size)
        fast.start()
        await _settle()
        events = _publish(bus, 0, 10)
        await _settle()
        # Joins behind the fast consumer and stops mid catch-up after its first event.
        slow = _Consumer(bus, "slow", start_cursor=-1, batch_size=batch_size, gate_after=1)
        slow.start()
        await _settle()
        events += _publish(bus, 10, 8)
        await _settle()
        events += _publish(bus, 18, 8)
        await _settle()
        held = (bus.released_upto, list(bus.buffer[:26]))
        slow.gate.set()
        await _settle()
        events += _publish(bus, 26, 6)  # first release after both consumers are at 25
        released = (bus.released_upto, list(bus.buffer[:32]))
        await _settle()
        await fast.stop()
        await slow.stop()
        return events, held, released, fast.items, slow.items

    events, held, released, fast_items, slow_items = asyncio.run(scenario())

    assert held == (-1, events[:26])
    assert released == (25, [None] * 26 + events[26:])
    assert fast_items == events
    assert slow_items == events


@_BOTH_READ_PATHS
def test_release_never_clears_a_slot_already_overwritten_by_a_newer_event(monkeypatch, batch_size):
    bus = _make_bus(monkeypatch, size=8, release_every="10")

    async def scenario():
        consumer = _Consumer(bus, "only", start_cursor=-1, batch_size=batch_size)
        consumer.start()
        await _settle()
        events = _publish(bus, 0, 8)
        await _settle()
        events += _publish(bus, 8, 2)  # seq 8, 9 overwrite slots 0, 1; first release runs at seq 9
        released = (bus.released_upto, list(bus.buffer))
        await _settle()
        await consumer.stop()
        return events, released, consumer.items

    events, released, items = asyncio.run(scenario())

    assert released == (7, [events[8], events[9], None, None, None, None, None, None])
    assert items == events


@_BOTH_READ_PATHS
def test_release_splits_a_range_that_wraps_past_the_end_of_the_ring(monkeypatch, batch_size):
    bus = _make_bus(monkeypatch, size=8, release_every="1")

    async def scenario():
        consumer = _Consumer(bus, "only", start_cursor=-1, batch_size=batch_size)
        consumer.start()
        await _settle()
        events = _publish(bus, 0, 6)
        await _settle()
        events += _publish(bus, 6, 1)
        await _settle()
        events += _publish(bus, 7, 3)
        await _settle()
        events += _publish(bus, 10, 1)  # releases seq 7..9: slots 7, 0, 1
        released = (bus.released_upto, list(bus.buffer))
        await _settle()
        await consumer.stop()
        return events, released, consumer.items

    events, released, items = asyncio.run(scenario())

    assert released == (9, [None, None, events[10], None, None, None, None, None])
    assert items == events


@_BOTH_READ_PATHS
def test_release_keeps_every_live_slot_and_every_event_accounted_over_many_wraps(monkeypatch, batch_size):
    size = 8
    bus = _make_bus(monkeypatch, size=size, release_every="3")
    chunks = [1, 2, 3, 1, 5, 7, 2, 9, 1, 1, 4, 13, 3, 2, 6, 1, 8, 8, 9, 5]

    async def scenario():
        consumer = _Consumer(bus, "only", start_cursor=-1, batch_size=batch_size)
        consumer.start()
        await _settle()
        events: list[_Ev] = []
        violations: list[str] = []
        for chunk in chunks:
            events += _publish(bus, len(events), chunk)
            await _settle()
            for seq in range(max(0, bus.cursor - size + 1), bus.cursor + 1):
                slot = bus.buffer[seq % size]
                expected = None if seq <= bus.released_upto else events[seq]
                if slot is not expected:
                    violations.append(f"seq={seq} slot={slot!r} released_upto={bus.released_upto}")
        await consumer.stop()
        return events, violations, consumer.items

    events, violations, items = asyncio.run(scenario())

    assert violations == []
    assert _accounted_seqs(items) == list(range(len(events)))
    assert any(isinstance(item, GapEvent) for item in items)  # the 13-event chunk lapped the 8-slot ring


@_BOTH_READ_PATHS
def test_never_seen_consumer_behind_the_watermark_gets_a_gap_but_no_overflow_count(monkeypatch, batch_size):
    telemetry = _Telemetry()
    guard = _Guard()
    bus = _make_bus(monkeypatch, size=16, telemetry=telemetry, storm_guard=guard, halt_threshold="1")

    async def scenario():
        steady = _Consumer(bus, "steady", start_cursor=-1, batch_size=batch_size)
        steady.start()
        await _settle()
        events = _publish(bus, 0, 10)
        await _settle()
        events += _publish(bus, 10, 1)  # releases seq 0..9
        late = _Consumer(bus, "late", start_cursor=-1, batch_size=batch_size)
        late.start()
        await _settle()
        before_more = list(late.items)
        events += _publish(bus, 11, 2)
        await _settle()
        await steady.stop()
        await late.stop()
        return events, before_more, late.items, steady.items

    events, before_more, late_items, steady_items = asyncio.run(scenario())

    gap = late_items[0]
    assert isinstance(gap, GapEvent)
    assert (gap.missed_count, gap.first_missed_seq, gap.last_missed_seq) == (10, 0, 9)
    assert before_more == late_items[:2]
    assert late_items[1:] == events[10:]
    assert steady_items == events
    assert telemetry.gap_event_counter.count == 1
    assert telemetry.overflow_counter.count == 0
    assert bus._overflow_count == 0
    assert bus._overflow_count_per_consumer == {}
    assert len(bus._overflow_timestamps) == 0
    assert guard.halts == []


@_BOTH_READ_PATHS
def test_consumer_behind_both_the_watermark_and_the_ring_gets_one_gap_for_the_whole_loss(monkeypatch, batch_size):
    telemetry = _Telemetry()
    bus = _make_bus(monkeypatch, size=8, telemetry=telemetry)

    async def scenario():
        first = _Consumer(bus, "first", start_cursor=-1, batch_size=batch_size)
        first.start()
        await _settle()
        events = _publish(bus, 0, 5)
        await _settle()
        events += _publish(bus, 5, 1)  # releases seq 0..4
        await first.stop()
        events += _publish(bus, 6, 20)  # nobody registered: no release, the ring laps to seq 25
        late = _Consumer(bus, "late", start_cursor=-1, batch_size=batch_size)
        late.start()
        await _settle()
        await late.stop()
        return events, late.items

    events, items = asyncio.run(scenario())

    gap = items[0]
    assert isinstance(gap, GapEvent)
    assert (gap.missed_count, gap.first_missed_seq, gap.last_missed_seq) == (18, 0, 17)
    assert items[1:] == events[18:]
    assert telemetry.overflow_counter.count == 1  # lapped by the ring: today's overflow accounting
    assert telemetry.gap_event_counter.count == 1


@_BOTH_READ_PATHS
def test_consumer_joining_at_the_latest_event_is_never_gapped_by_the_watermark(monkeypatch, batch_size):
    bus = _make_bus(monkeypatch, size=16)

    async def scenario():
        steady = _Consumer(bus, "steady", start_cursor=-1, batch_size=batch_size)
        steady.start()
        await _settle()
        _publish(bus, 0, 6)
        await _settle()
        _publish(bus, 6, 1)  # releases seq 0..5
        late = _Consumer(bus, "late", batch_size=batch_size)
        late.start()
        await _settle()
        events = _publish(bus, 7, 2)
        await _settle()
        await steady.stop()
        await late.stop()
        return events, late.items

    events, items = asyncio.run(scenario())

    assert bus.released_upto >= 5
    assert items == events


@_BOTH_READ_PATHS
def test_slot_released_under_a_same_named_consumer_is_reported_as_a_gap_not_skipped(monkeypatch, batch_size):
    bus = _make_bus(monkeypatch, size=64)

    async def scenario():
        events = _publish(bus, 0, 8)
        # Both register under one name, so the shared position follows whichever wrote last.
        behind = _Consumer(bus, "dup", start_cursor=-1, batch_size=batch_size, gate_after=1)
        behind.start()
        await _settle()
        ahead = _Consumer(bus, "dup", start_cursor=-1, batch_size=batch_size)
        ahead.start()
        await _settle()
        events += _publish(bus, 8, 1)  # releases seq 0..7 while `behind` still has seq 1..7 to read
        released_upto = bus.released_upto
        behind.gate.set()
        await _settle()
        await ahead.stop()
        await behind.stop()
        return events, released_upto, behind.items

    events, released_upto, items = asyncio.run(scenario())

    read_before_release = batch_size or 1  # a batch consumer already holds its first batch; consume() one event
    assert released_upto == 7
    assert items[:read_before_release] == events[:read_before_release]
    gap = items[read_before_release]
    assert isinstance(gap, GapEvent)
    assert (gap.missed_count, gap.first_missed_seq, gap.last_missed_seq) == (
        8 - read_before_release,
        read_before_release,
        7,
    )
    assert items[read_before_release + 1 :] == events[8:]


@pytest.mark.parametrize("enabled", [True, False], ids=["release_on", "release_off"])
def test_released_event_object_is_freed_without_a_gc_pass_only_when_release_is_on(monkeypatch, gc_off, enabled):
    bus = _make_bus(monkeypatch, size=64, release="1" if enabled else "0")

    async def scenario():
        refs: list[weakref.ref[_Ev]] = []
        consumer_seen = 0

        async def drain():
            nonlocal consumer_seen
            async for _event in bus.consume(start_cursor=-1, consumer_name="only"):
                consumer_seen += 1

        task = asyncio.ensure_future(drain())
        await _settle()
        for event in _publish(bus, 0, 10):
            refs.append(weakref.ref(event))
        del event
        await _settle()
        _publish(bus, 10, 1)  # releases seq 0..9
        alive = [ref() is not None for ref in refs]
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return alive, consumer_seen

    alive, consumer_seen = asyncio.run(scenario())

    assert consumer_seen == 10
    # seq 9 is still referenced by the consumer's own frame; every earlier one is only referenced by the ring.
    assert alive[:9] == ([False] * 9 if enabled else [True] * 9)


def test_release_runs_exactly_at_the_default_cadence_of_256_publishes(monkeypatch):
    monkeypatch.delenv("HFT_BUS_RELEASE_EVERY", raising=False)
    monkeypatch.delenv("HFT_BUS_RELEASE_CONSUMED", raising=False)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", False)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", False)
    monkeypatch.setattr(event_bus_mod, "_WAIT_MODE", "event")
    bus = event_bus_mod.RingBufferBus(size=1024)

    async def scenario():
        consumer = _Consumer(bus, "only", start_cursor=-1)
        consumer.start()
        await _settle()
        _publish(bus, 0, 100)
        await _settle()  # the consumer's position is 99
        _publish(bus, 100, 155)  # 255 publishes in total
        before = bus.released_upto
        _publish(bus, 255, 1)  # the 256th publish
        after = bus.released_upto
        await consumer.stop()
        return before, after

    before, after = asyncio.run(scenario())

    assert (before, after) == (-1, 99)
    assert bus.buffer[:100] == [None] * 100
    assert bus.buffer[100] is not None


def test_release_every_below_one_is_clamped_to_every_publish(monkeypatch):
    bus = _make_bus(monkeypatch, size=16, release_every="0")

    async def scenario():
        consumer = _Consumer(bus, "only", start_cursor=-1)
        consumer.start()
        await _settle()
        _publish(bus, 0, 3)
        await _settle()
        _publish(bus, 3, 1)
        released_upto = bus.released_upto
        await consumer.stop()
        return released_upto

    assert asyncio.run(scenario()) == 2


@pytest.mark.parametrize("single_writer", ["1", "0"], ids=["single_writer", "multi_writer"])
@pytest.mark.parametrize("how", ["publish", "publish_many", "publish_nowait", "publish_many_nowait"])
def test_release_runs_on_every_publish_path(monkeypatch, single_writer, how):
    bus = _make_bus(monkeypatch, size=16, single_writer=single_writer)

    async def publish(events: list[_Ev]) -> None:
        if how == "publish":
            for event in events:
                await bus.publish(event)
        elif how == "publish_many":
            await bus.publish_many(events)
        elif how == "publish_nowait":
            for event in events:
                bus.publish_nowait(event)
        else:
            bus.publish_many_nowait(events)

    async def scenario():
        consumer = _Consumer(bus, "only", start_cursor=-1)
        consumer.start()
        await _settle()
        events = [_Ev(seq) for seq in range(5)]
        await publish(events)
        await _settle()
        events.append(_Ev(5))
        await publish(events[5:])
        slots = list(bus.buffer[:6])
        await consumer.stop()
        return events, slots

    events, slots = asyncio.run(scenario())

    assert bus.released_upto == 4
    assert slots == [None] * 5 + [events[5]]


def test_release_leaves_the_typed_tick_ring_untouched(monkeypatch):
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", True)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", False)
    monkeypatch.setattr(event_bus_mod, "_WAIT_MODE", "event")
    monkeypatch.setenv("HFT_BUS_RELEASE_EVERY", "1")
    monkeypatch.setenv("HFT_BUS_RELEASE_CONSUMED", "1")
    bus = event_bus_mod.RingBufferBus(size=16)

    async def scenario():
        consumer = _Consumer(bus, "only", start_cursor=-1)
        consumer.start()
        await _settle()
        for seq in range(5):
            bus.publish_nowait(("tick", "TXFA6", 100 + seq, 1, seq, False, False, seq))
        await _settle()
        bus.publish_nowait(("tick", "TXFA6", 105, 1, 5, False, False, 5))
        await consumer.stop()
        return consumer.items

    items = asyncio.run(scenario())

    assert bus.released_upto == 4
    assert bus._tick_ring is not None
    assert all(slot is not None for slot in bus._tick_ring.buffer[:6])
    assert [item[2] for item in items] == [100, 101, 102, 103, 104]


def test_released_upto_is_read_only(monkeypatch):
    bus = _make_bus(monkeypatch)

    attribute = "released_upto"

    with pytest.raises(AttributeError):
        setattr(bus, attribute, 3)

    assert bus.released_upto == -1


async def _crash_recorder_after_five_events(bus, batch_size, *, extra: int):
    """recorder_bridge and strategy_runner read seq 0..4; the recorder's generator then closes (a crash)."""
    recorder = _Consumer(bus, "recorder_bridge", start_cursor=-1, batch_size=batch_size)
    runner = _Consumer(bus, "strategy_runner", start_cursor=-1, batch_size=batch_size)
    recorder.start()
    runner.start()
    await _settle()
    events = _publish(bus, 0, 5)
    await _settle()
    await recorder.stop()
    events += _publish(bus, 5, extra)
    await _settle()
    return events, recorder, runner


@_BOTH_READ_PATHS
def test_closed_consumer_still_holds_the_release_at_its_last_position(monkeypatch, batch_size):
    bus = _make_bus(monkeypatch, size=64)

    async def scenario():
        events, recorder, runner = await _crash_recorder_after_five_events(bus, batch_size, extra=20)
        events += _publish(bus, 25, 1)
        await runner.stop()
        return events, recorder.items, runner.items

    events, recorder_items, runner_items = asyncio.run(scenario())

    assert "recorder_bridge" not in bus._consumer_positions
    assert bus.released_upto == 4
    assert bus.buffer[:5] == [None] * 5
    assert bus.buffer[5:26] == events[5:26]
    assert recorder_items == events[:5]
    assert runner_items == events[:25]


@_BOTH_READ_PATHS
def test_restarted_consumer_resumes_after_its_old_position_without_a_gap_or_overflow(monkeypatch, batch_size):
    telemetry = _Telemetry()
    guard = _Guard()
    log = _RecordingLogger()
    monkeypatch.setattr(event_bus_mod, "logger", log)
    bus = _make_bus(monkeypatch, size=64, telemetry=telemetry, storm_guard=guard, halt_threshold="1")

    async def scenario():
        events, recorder, runner = await _crash_recorder_after_five_events(bus, batch_size, extra=20)
        restarted = _Consumer(bus, "recorder_bridge", start_cursor=-1, batch_size=batch_size)
        restarted.start()
        await _settle()
        events += _publish(bus, 25, 3)
        await _settle()
        await restarted.stop()
        await runner.stop()
        return events, recorder.items, restarted.items

    events, first_life, restarted_items = asyncio.run(scenario())

    assert first_life == events[:5]
    assert restarted_items == events[5:]
    assert not any(isinstance(item, GapEvent) for item in restarted_items)
    assert telemetry.overflow_counter.count == 0
    assert telemetry.gap_event_counter.count == 0
    assert bus._overflow_count == 0
    assert len(bus._overflow_timestamps) == 0
    assert guard.halts == []
    assert log.events("bus_consumer_rejoined_after_release") == [
        (
            "info",
            "bus_consumer_rejoined_after_release",
            {"consumer": "recorder_bridge", "requested_start": -1, "start": 4},
        )
    ]


@_BOTH_READ_PATHS
def test_forget_consumer_lets_the_release_advance_past_a_closed_consumer(monkeypatch, batch_size):
    bus = _make_bus(monkeypatch, size=64)

    async def scenario():
        events, recorder, runner = await _crash_recorder_after_five_events(bus, batch_size, extra=20)
        held = bus.released_upto
        bus.forget_consumer("recorder_bridge")
        bus.forget_consumer("never_seen")
        events += _publish(bus, 25, 1)
        released = (bus.released_upto, list(bus.buffer[:26]))
        await _settle()
        await runner.stop()
        return events, held, released, runner.items

    events, held, released, runner_items = asyncio.run(scenario())

    assert held == 4
    assert released == (24, [None] * 25 + [events[25]])
    assert runner_items == events


@_BOTH_READ_PATHS
def test_consumer_lapped_by_the_ring_is_still_counted_as_an_overflow_and_a_halt_input(monkeypatch, batch_size):
    telemetry = _Telemetry()
    guard = _Guard()
    bus = _make_bus(monkeypatch, size=8, telemetry=telemetry, storm_guard=guard, halt_threshold="1")

    async def scenario():
        lagging = _Consumer(bus, "lagging", start_cursor=-1, batch_size=batch_size)
        lagging.start()
        await _settle()
        events = _publish(bus, 0, 20)  # 20 events before the consumer can run: it is lapped by the 8-slot ring
        await _settle()
        await lagging.stop()
        return events, lagging.items

    events, items = asyncio.run(scenario())

    gap = items[0]
    assert isinstance(gap, GapEvent)
    assert (gap.missed_count, gap.first_missed_seq, gap.last_missed_seq) == (12, 0, 11)
    assert items[1:] == events[12:]
    assert telemetry.overflow_counter.count == 1
    assert telemetry.gap_event_counter.count == 1
    assert len(bus._overflow_timestamps) == 1
    assert len(guard.halts) == 1
    assert guard.halts[0].startswith(
        "EventBus batch overflow: consumer=lagging" if batch_size else "EventBus overflow: consumer=lagging"
    )


def test_shutdown_drain_still_finds_events_a_crashed_runner_never_processed(monkeypatch):
    bus = _make_bus(monkeypatch, size=64)
    processed: list[Any] = []

    async def process_event(event: Any) -> None:
        processed.append(event)

    async def scenario():
        events, recorder, runner = await _crash_recorder_after_five_events(bus, 0, extra=0)
        await runner.stop()  # the runner's generator closes after seq 0..4; the recorder keeps going
        recorder_again = _Consumer(bus, "recorder_bridge", start_cursor=-1)
        recorder_again.start()
        await _settle()
        for first_seq in (5, 15, 25):
            events += _publish(bus, first_seq, 10)  # the recorder catches up between chunks
            await _settle()
        drain_self = SimpleNamespace(_consumer_seq=4, bus=bus, process_event=process_event)
        result = await StrategyRunner.drain_to_cursor(drain_self, bus.cursor, 5.0)
        await recorder_again.stop()
        return events, result

    events, result = asyncio.run(scenario())

    assert bus.released_upto == 4
    assert result == (30, 0)
    assert processed == events[5:]
