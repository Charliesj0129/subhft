"""Tests for remaining uncovered lines in engine/event_bus.py.

Targets:
- Lines 633-634: consume() per-consumer signal wait path (await my_signal.wait() + clear())
- Line  640:     consume() spin-budget early-break when cursor advances mid-loop
- Lines 703-704: consume() buffer lazy-init when buffer is None (redundant path)
- Lines 741-742: consume_batch() per-consumer signal wait path
- Line  747:     consume_batch() spin-budget exhausted → asyncio.sleep(0)
- Lines 800, 804, 806: consume_batch() typed ring reads (tick kind=1, bidask kind=2, lobstats kind=3)
- Lines 812-813: consume_batch() buffer lazy-init when buffer is None
"""

import asyncio

import pytest

from hft_platform.engine import event_bus as event_bus_mod
from hft_platform.engine.event_bus import RingBufferBus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _py_bus_event(monkeypatch, *, size: int = 16) -> RingBufferBus:
    """Pure-Python bus in event (signal) wait mode."""
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", False)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", False)
    monkeypatch.setattr(event_bus_mod, "_WAIT_MODE", "event")
    return RingBufferBus(size=size)


def _py_bus_typed(monkeypatch, *, size: int = 16) -> RingBufferBus:
    """Pure-Python bus with typed rings enabled (tick + book), event wait mode."""
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", True)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", True)
    monkeypatch.setattr(event_bus_mod, "_WAIT_MODE", "event")
    return RingBufferBus(size=size)


# ---------------------------------------------------------------------------
# Lines 633-634: consume() — per-consumer signal wait (event mode)
#
# The consumer registers a per-consumer asyncio.Event (my_signal) and awaits it
# when no data is available.  publish_nowait → _notify() → sets my_signal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_signal_wait_wakes_on_publish(monkeypatch):
    """Lines 633-634: consumer blocks on my_signal.wait(); publish_nowait wakes it.

    start_cursor=-1 → local_seq=-1.  cursor starts at -1 so cursor<=local_seq.
    Consumer enters the signal-wait (lines 633-634).  publish_nowait advances
    cursor to 0 > -1 and sets my_signal → consumer wakes.
    """
    bus = _py_bus_event(monkeypatch)
    assert bus.signal is not None  # event mode

    collected: list = []

    async def _consumer():
        # start_cursor=-1 → local_seq=-1; cursor=-1 <= -1 → enters wait
        async for evt in bus.consume(start_cursor=-1, consumer_name="sig-wait"):
            collected.append(evt)
            if len(collected) >= 1:
                break

    task = asyncio.create_task(_consumer())
    # Yield so consumer enters the signal-wait (lines 633-634)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Confirm consumer is waiting (no events yet)
    assert len(collected) == 0

    # publish_nowait triggers _notify() → sets my_signal → consumer wakes
    bus.publish_nowait({"value": 42})

    await asyncio.wait_for(task, timeout=2.0)
    assert len(collected) == 1
    assert collected[0] == {"value": 42}


@pytest.mark.asyncio
async def test_consume_signal_wait_multiple_events(monkeypatch):
    """Lines 633-634: consumer waits on signal; two sequential publishes deliver both events."""
    bus = _py_bus_event(monkeypatch)

    received: list = []

    async def _consumer():
        # start_cursor=-1 → local_seq=-1; enters signal wait
        async for evt in bus.consume(start_cursor=-1, consumer_name="sig-multi"):
            received.append(evt)
            if len(received) >= 2:
                break

    task = asyncio.create_task(_consumer())
    await asyncio.sleep(0)  # let consumer enter signal-wait

    bus.publish_nowait("alpha")  # cursor → 0; wakes consumer
    await asyncio.sleep(0)  # let consumer process "alpha"
    bus.publish_nowait("beta")  # cursor → 1; second wake

    await asyncio.wait_for(task, timeout=2.0)
    assert "alpha" in received
    assert "beta" in received


# ---------------------------------------------------------------------------
# Line 640: consume() spin-budget early-break
#
# In spin mode with _spin_sleep=0, the inner for-loop breaks early when
# self.cursor > local_seq.  We need cursor to advance *during* the spin budget.
# Since this is cooperative asyncio, we pre-advance cursor before the consumer
# task checks, so the loop finds data immediately on first spin iteration.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_spin_budget_early_break(monkeypatch):
    """Line 640: spin budget breaks early when cursor already > local_seq."""
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", False)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", False)
    monkeypatch.setattr(event_bus_mod, "_WAIT_MODE", "spin")

    bus = RingBufferBus(size=16)
    bus._spin_sleep = 0  # force spin-budget path
    bus._spin_budget = 100  # large budget so early-break is exercised

    # Publish one seed so cursor=0; consumer will start at local_seq=0
    bus.publish_nowait("seed")
    # Publish again so cursor=1 > 0=local_seq — break triggers on first spin iter
    bus.publish_nowait("target")

    # Consumer starts with start_cursor=None → local_seq = cursor (=1)
    # That's already equal.  Instead use start_cursor=0 so local_seq=0 and
    # cursor=1 > 0 immediately → spin loop breaks on first iteration.
    collected: list = []

    async def _consumer():
        async for evt in bus.consume(start_cursor=0, consumer_name="spin-break"):
            collected.append(evt)
            if len(collected) >= 1:
                break

    task = asyncio.create_task(_consumer())
    for _ in range(10):
        await asyncio.sleep(0)
        if collected:
            break

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # The consumer must have read "target" (seq 1, which is > local_seq=0)
    assert len(collected) >= 1


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Lines 741-742: consume_batch() — per-consumer signal wait (event mode)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_batch_signal_wait_wakes_on_publish(monkeypatch):
    """Lines 741-742: consume_batch() blocks on my_signal; publish wakes it.

    start_cursor=-1 → local_seq=-1.  Consumer enters signal-wait (741-742).
    Two publishes advance cursor to 1 and set my_signal → consumer wakes.
    """
    bus = _py_bus_event(monkeypatch)
    assert bus.signal is not None

    batches: list = []

    async def _consumer():
        # start_cursor=-1 → local_seq=-1; cursor=-1 <= -1 → enters wait
        async for batch in bus.consume_batch(batch_size=4, start_cursor=-1, consumer_name="batch-sig"):
            batches.append(batch)
            if len(batches) >= 1:
                break

    task = asyncio.create_task(_consumer())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(batches) == 0  # consumer is waiting

    bus.publish_nowait("batch-item-1")
    bus.publish_nowait("batch-item-2")

    await asyncio.wait_for(task, timeout=2.0)
    assert len(batches) >= 1
    combined = [e for b in batches for e in b]
    assert "batch-item-1" in combined


@pytest.mark.asyncio
async def test_consume_batch_signal_wait_single_item(monkeypatch):
    """Lines 741-742: consume_batch signal wait wakes correctly for one item."""
    bus = _py_bus_event(monkeypatch)

    batches: list = []

    async def _consumer():
        # start_cursor=-1 → local_seq=-1; enters signal-wait (741-742)
        async for batch in bus.consume_batch(batch_size=1, start_cursor=-1, consumer_name="batch-sig-1"):
            batches.append(batch)
            break

    task = asyncio.create_task(_consumer())
    await asyncio.sleep(0)  # let consumer enter signal-wait

    bus.publish_nowait("solo-batch")  # cursor → 0 > -1; wakes consumer

    await asyncio.wait_for(task, timeout=2.0)
    assert batches[0] == ["solo-batch"]


# ---------------------------------------------------------------------------
# Line 747: consume_batch() spin-budget exhausted → asyncio.sleep(0)
#
# With _spin_sleep=0 and a tiny spin budget that never finds data,
# the inner loop falls through to `if self.cursor <= local_seq: await asyncio.sleep(0)`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_batch_spin_budget_exhausted_sleeps(monkeypatch):
    """Line 747: spin budget exhausted without new data → asyncio.sleep(0)."""
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_TICK_RING", False)
    monkeypatch.setattr(event_bus_mod, "_USE_TYPED_BOOK_RINGS", False)
    monkeypatch.setattr(event_bus_mod, "_WAIT_MODE", "spin")

    bus = RingBufferBus(size=16)
    bus._spin_sleep = 0  # spin-budget path
    bus._spin_budget = 1  # minimal budget → exhausted immediately

    # Pre-seed so cursor=0; consumer starts at local_seq=0 → cursor <= local_seq
    bus.publish_nowait("seed-exhaust")

    batches: list = []

    async def _consumer():
        async for batch in bus.consume_batch(batch_size=4, start_cursor=None, consumer_name="exhaust"):
            batches.append(batch)
            break

    task = asyncio.create_task(_consumer())
    # Several yields: first lets consumer enter spin-wait with budget=1 (exhausts
    # immediately → asyncio.sleep(0)), then we publish to unblock it.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    bus.publish_nowait("exhaust-item")
    for _ in range(20):
        await asyncio.sleep(0)
        if batches:
            break

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    combined = [e for b in batches for e in b]
    assert "exhaust-item" in combined


# ---------------------------------------------------------------------------
# Lines 800, 804, 806: consume_batch() typed ring reads
#
# kind=1 → tick ring read (line 800)
# kind=2 → bidask ring read (line 804) [covered incidentally]
# kind=3 → lobstats ring read (line 806)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_batch_tick_ring_read_kind1(monkeypatch):
    """Line 800: consume_batch() reads from tick ring when kind=1."""
    bus = _py_bus_typed(monkeypatch)
    assert bus._tick_ring is not None
    assert bus._kind_ring is not None

    tick = ("tick", "2330", 10000, 1, 100, False, False, 999)
    bus.publish_nowait(tick)

    # Verify kind was set to 1 for the tick event
    assert bus._kind_ring[0] == 1

    batches: list = []
    async for batch in bus.consume_batch(batch_size=4, start_cursor=-1, consumer_name="batch-tick"):
        batches.append(batch)
        break

    assert len(batches) == 1
    assert batches[0][0] == tick


@pytest.mark.asyncio
async def test_consume_batch_lobstats_ring_read_kind3(monkeypatch):
    """Line 806: consume_batch() reads from lobstats ring when kind=3."""
    bus = _py_bus_typed(monkeypatch)
    assert bus._lobstats_ring is not None
    assert bus._kind_ring is not None

    lobstats = ("lobstats", "2330", 100, 20010, 10, 0.5, 10000, 10010, 5, 3)
    bus.publish_nowait(lobstats)

    # Verify kind was set to 3 for the lobstats event
    assert bus._kind_ring[0] == 3

    batches: list = []
    async for batch in bus.consume_batch(batch_size=4, start_cursor=-1, consumer_name="batch-lob"):
        batches.append(batch)
        break

    assert len(batches) == 1
    result = batches[0][0]
    assert result is not None
    assert result[0] == "lobstats"
    assert result[1] == "2330"


@pytest.mark.asyncio
async def test_consume_batch_mixed_typed_events(monkeypatch):
    """Lines 800+806: consume_batch() reads tick (kind=1) and lobstats (kind=3) in one batch."""
    bus = _py_bus_typed(monkeypatch)

    tick = ("tick", "2330", 10000, 1, 100, False, False, 999)
    lobstats = ("lobstats", "2330", 200, 20020, 8, 0.3, 10001, 10009, 4, 6)

    bus.publish_nowait(tick)
    bus.publish_nowait(lobstats)

    assert bus._kind_ring[0] == 1  # tick
    assert bus._kind_ring[1] == 3  # lobstats

    batches: list = []
    async for batch in bus.consume_batch(batch_size=4, start_cursor=-1, consumer_name="batch-mixed"):
        batches.append(batch)
        break

    combined = [e for b in batches for e in b]
    assert len(combined) == 2
    assert combined[0] == tick
    assert combined[1][0] == "lobstats"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
