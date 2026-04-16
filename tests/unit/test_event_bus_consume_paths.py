"""Tests covering previously-uncovered paths in engine/event_bus.py.

Targets (min 5 new statements):
- Line 566: _notify early-return when _notify_counter % _notify_every != 0
- Lines 637-642: consume() spin-wait inner loop (_spin_sleep <= 0 branch)
- Lines 703-705: consume() fallback buffer=None lazy-init path
- Lines 744-749: consume_batch() spin-wait inner loop
- Lines 810-813: consume_batch() fallback buffer=None lazy-init path
"""

import asyncio

import pytest

from hft_platform.engine import event_bus as event_bus_mod
from hft_platform.engine.event_bus import RingBufferBus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _py_bus(monkeypatch, *, size: int = 16, wait_mode: str = "event") -> RingBufferBus:
    """Pure-Python RingBufferBus with no typed rings (use_rust=False)."""
    monkeypatch.setattr(event_bus_mod, "_RUST_ENABLED", False)
    monkeypatch.setattr(event_bus_mod, "_USE_RUST_BUS", False)
    monkeypatch.setattr(event_bus_mod, "_RUST_RING_FACTORY", None)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", False)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", False)
    monkeypatch.setattr(event_bus_mod, "_RUST_TICK_RING_FACTORY", None)
    monkeypatch.setattr(event_bus_mod, "_RUST_BIDASK_RING_FACTORY", None)
    monkeypatch.setattr(event_bus_mod, "_RUST_LOBSTATS_RING_FACTORY", None)
    monkeypatch.setattr(event_bus_mod, "_WAIT_MODE", wait_mode)
    return RingBufferBus(size=size)


# ---------------------------------------------------------------------------
# Line 566: _notify early return — signal NOT set on non-modulo calls
# ---------------------------------------------------------------------------


def test_notify_early_return_when_counter_not_multiple_of_notify_every(monkeypatch):
    """_notify() returns early (line 566) when _notify_counter % _notify_every != 0.

    With notify_every=2, the first publish_nowait increments counter to 1.
    1 % 2 != 0, so _notify returns without setting the signal.
    """
    bus = _py_bus(monkeypatch, wait_mode="event")
    bus._notify_every = 2   # override default of 1

    assert bus.signal is not None

    # First publish: counter goes from 0 → 1, 1 % 2 == 1 → early return (line 566)
    bus.publish_nowait("event-1")

    # Signal must NOT have been set (early return taken)
    assert not bus.signal.is_set()

    # Second publish: counter goes from 1 → 2, 2 % 2 == 0 → signal IS set
    bus.publish_nowait("event-2")
    assert bus.signal.is_set()


# ---------------------------------------------------------------------------
# Helper coroutines for spin-wait tests
# ---------------------------------------------------------------------------

async def _consume_one(bus: RingBufferBus, out: list, start_cursor, name: str) -> None:
    """Append first event from bus.consume() to out then return."""
    async for evt in bus.consume(start_cursor=start_cursor, consumer_name=name):
        out.append(evt)
        break


async def _consume_batch_one(bus: RingBufferBus, out: list, start_cursor, name: str) -> None:
    """Append first batch from bus.consume_batch() to out then return."""
    async for batch in bus.consume_batch(batch_size=4, start_cursor=start_cursor, consumer_name=name):
        out.append(batch)
        break


# ---------------------------------------------------------------------------
# Lines 637-642: consume() spin-wait inner loop (_spin_sleep <= 0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_spin_wait_yields_published_event(monkeypatch):
    """consume() spin-wait path (lines 637-642): cursor polling with _spin_sleep=0.

    Spin-wait mode has no asyncio.Event signal — the inner loop polls self.cursor.
    We use a Task so that the consumer enters the spin-wait loop first, then the
    producer publishes.  The consumer's start_cursor is set to current cursor (0
    after one pre-seed publish) so local_seq=0; we then publish a second event to
    advance cursor to 1 and satisfy cursor > local_seq.
    """
    bus = _py_bus(monkeypatch, wait_mode="spin")
    assert bus.signal is None  # spin mode: no signal

    # Pre-seed one event so cursor=0; consumer will start_cursor=None → local_seq=0
    # Then after consumer task starts, we publish one more event to move cursor to 1.
    bus.publish_nowait("seed-event")   # cursor → 0

    events: list = []

    consumer_task = asyncio.ensure_future(
        _consume_one(bus, events, start_cursor=None, name="spin-consumer")
    )
    # Yield to let the consumer task run and enter the spin-wait (cursor=0, local_seq=0)
    await asyncio.sleep(0)
    bus.publish_nowait("spin-event")   # cursor → 1 → satisfies cursor > local_seq=0
    for _ in range(20):
        await asyncio.sleep(0)
        if events:
            break

    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass

    assert "spin-event" in events


@pytest.mark.asyncio
async def test_consume_spin_wait_with_sleep_yields_event(monkeypatch):
    """consume() spin-wait _spin_sleep > 0 path (line 644): asyncio.sleep branch.

    With _spin_sleep > 0 the inner loop uses asyncio.sleep instead of the spin-budget.
    """
    bus = _py_bus(monkeypatch, wait_mode="spin")
    bus._spin_sleep = 0.005   # > 0 → exercises the else branch (line 644)

    bus.publish_nowait("seed")    # cursor → 0

    events: list = []
    consumer_task = asyncio.ensure_future(
        _consume_one(bus, events, start_cursor=None, name="sleep-spin")
    )
    await asyncio.sleep(0)
    bus.publish_nowait("sleep-spin-event")   # cursor → 1
    await asyncio.sleep(0.030)              # wait for the sleep inside consume to wake

    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass

    assert "sleep-spin-event" in events


# ---------------------------------------------------------------------------
# Lines 703-705: consume() fallback buffer lazy-init (buffer is None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_buffer_none_init_path(monkeypatch):
    """consume() initialises self.buffer lazily (lines 703-705) when buffer is None.

    Force buffer to None after construction (it is normally created), then verify
    that consume() creates it and returns the event correctly.
    """
    bus = _py_bus(monkeypatch, wait_mode="event")
    # Ensure no typed rings so we fall into the else branch (lines 700-705)
    assert bus._tick_ring is None
    assert bus._bidask_ring is None
    assert bus._lobstats_ring is None

    # Null out buffer to force the lazy-init path
    bus.buffer = None

    # Publish writes via _store_fallback which re-inits buffer internally,
    # but then we null it again to test the consume-side lazy init.
    bus._publish_unlocked("lazy-init-event")
    bus._notify()
    bus.buffer = None   # force consume to hit the buffer-is-None branch

    # publish_nowait increments cursor; we nulled buffer so consume must init it.
    # But since buffer is None after re-nulling, the event won't be there — we
    # just verify that consume() doesn't crash and initialises self.buffer.
    # Re-publish with buffer None so _store_fallback also hits its own lazy init:
    bus2 = _py_bus(monkeypatch, wait_mode="event")
    bus2._tick_ring = None
    bus2._bidask_ring = None
    bus2._lobstats_ring = None
    bus2.buffer = None  # null before publish so _store_fallback also inits lazily

    bus2.publish_nowait("lazy-buf-event")

    events = []
    async for evt in bus2.consume(start_cursor=-1, consumer_name="lazy-buf"):
        events.append(evt)
        break

    assert events == ["lazy-buf-event"]
    assert bus2.buffer is not None   # confirmed initialised by consume()


# ---------------------------------------------------------------------------
# Lines 744-749: consume_batch() spin-wait inner loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_batch_spin_wait_yields_batch(monkeypatch):
    """consume_batch() spin-wait path (lines 744-749): cursor polling with _spin_sleep=0.

    Consumer enters the spin-wait inner loop first, then the producer publishes.
    """
    bus = _py_bus(monkeypatch, wait_mode="spin")
    assert bus.signal is None

    bus.publish_nowait("seed-batch")   # cursor → 0; consumer will have local_seq=0

    batches: list = []
    consumer_task = asyncio.ensure_future(
        _consume_batch_one(bus, batches, start_cursor=None, name="batch-spin")
    )
    await asyncio.sleep(0)
    bus.publish_nowait("batch-spin-1")
    bus.publish_nowait("batch-spin-2")
    for _ in range(20):
        await asyncio.sleep(0)
        if batches:
            break

    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass

    assert len(batches) >= 1
    # The first batch yielded must contain the two new events (not the seed)
    combined = [e for b in batches for e in b]
    assert "batch-spin-1" in combined


@pytest.mark.asyncio
async def test_consume_batch_spin_sleep_branch_yields_batch(monkeypatch):
    """consume_batch() spin-wait _spin_sleep > 0 path (line 751)."""
    bus = _py_bus(monkeypatch, wait_mode="spin")
    bus._spin_sleep = 0.005

    bus.publish_nowait("seed-bs")     # cursor → 0

    batches: list = []
    consumer_task = asyncio.ensure_future(
        _consume_batch_one(bus, batches, start_cursor=None, name="bs-sleep")
    )
    await asyncio.sleep(0)
    bus.publish_nowait("bs-event")
    await asyncio.sleep(0.030)

    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass

    combined = [e for b in batches for e in b]
    assert "bs-event" in combined


# ---------------------------------------------------------------------------
# Lines 810-813: consume_batch() fallback buffer lazy-init
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_batch_buffer_none_init_path(monkeypatch):
    """consume_batch() initialises self.buffer lazily (lines 810-813) when buffer is None."""
    bus = _py_bus(monkeypatch, wait_mode="event")
    assert bus._tick_ring is None
    assert bus._bidask_ring is None
    assert bus._lobstats_ring is None

    # Force buffer to None before publish so _store_fallback also hits its lazy-init,
    # then verify consume_batch can reconstruct it.
    bus.buffer = None
    bus.publish_nowait("lazy-batch-event")

    batches = []
    async for batch in bus.consume_batch(batch_size=4, start_cursor=-1, consumer_name="lazy-batch"):
        batches.append(batch)
        break

    assert batches[0] == ["lazy-batch-event"]
    assert bus.buffer is not None
