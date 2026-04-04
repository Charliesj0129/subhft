"""Tests for GapEvent injection on RingBufferBus overflow and strategy dispatch."""

import asyncio

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.events import GapEvent
from hft_platform.strategy.base import BaseStrategy, StrategyContext


class _GapAwareStrategy(BaseStrategy):
    """Test strategy that records GapEvent dispatches."""

    def __init__(self) -> None:
        super().__init__(strategy_id="test-gap")
        self.gap_events: list[GapEvent] = []

    def on_gap(self, event: GapEvent) -> None:
        self.gap_events.append(event)


def test_gap_event_slots() -> None:
    """GapEvent must use __slots__ per HFT convention."""
    ge = GapEvent(missed_count=10, first_missed_seq=5, last_missed_seq=14, ts=123456)
    assert not hasattr(ge, "__dict__"), "GapEvent must use __slots__"
    assert ge.missed_count == 10
    assert ge.first_missed_seq == 5
    assert ge.last_missed_seq == 14
    assert ge.ts == 123456


def test_consume_overflow_yields_gap_event() -> None:
    """When consumer detects overflow, a GapEvent must be yielded before normal events."""
    bus = RingBufferBus(size=4)

    # Publish more events than buffer size to trigger overflow for a stale consumer
    for i in range(10):
        bus.publish_nowait(f"evt-{i}")

    collected: list = []

    async def _run() -> None:
        count = 0
        async for evt in bus.consume(start_cursor=-1):
            collected.append(evt)
            count += 1
            if count >= 5:
                break

    asyncio.run(_run())

    gap_events = [e for e in collected if isinstance(e, GapEvent)]
    assert len(gap_events) >= 1, "Expected at least one GapEvent on overflow"

    ge = gap_events[0]
    assert ge.missed_count > 0
    assert ge.first_missed_seq >= 0
    assert ge.last_missed_seq >= ge.first_missed_seq
    assert ge.ts > 0


def test_consume_overflow_gap_event_metric() -> None:
    """bus_gap_events_total metric must increment on overflow."""
    bus = RingBufferBus(size=2)
    initial = bus.metrics.bus_gap_events_total._value.get()

    for i in range(10):
        bus.publish_nowait(f"evt-{i}")

    async def _run() -> None:
        async for evt in bus.consume(start_cursor=-1):
            return

    asyncio.run(_run())
    assert bus.metrics.bus_gap_events_total._value.get() > initial


def test_consume_batch_overflow_yields_gap_event() -> None:
    """consume_batch must also yield GapEvent on overflow."""
    bus = RingBufferBus(size=4)

    for i in range(10):
        bus.publish_nowait(f"evt-{i}")

    collected: list = []

    async def _run() -> None:
        async for batch in bus.consume_batch(batch_size=8, start_cursor=-1):
            collected.extend(batch)
            # First batch is the GapEvent, second batch has real events.
            # After two yields all available data is consumed; break to avoid hang.
            has_gap = any(isinstance(e, GapEvent) for e in collected)
            has_normal = any(isinstance(e, str) for e in collected)
            if has_gap and has_normal:
                break

    asyncio.run(_run())

    gap_events = [e for e in collected if isinstance(e, GapEvent)]
    assert len(gap_events) >= 1, "Expected at least one GapEvent in batch on overflow"


def test_consume_no_gap_event_without_overflow() -> None:
    """When no overflow occurs, no GapEvent should be yielded."""
    bus = RingBufferBus(size=64)

    bus.publish_nowait("a")
    bus.publish_nowait("b")

    collected: list = []

    async def _run() -> None:
        count = 0
        async for evt in bus.consume(start_cursor=-1):
            collected.append(evt)
            count += 1
            if count >= 2:
                break

    asyncio.run(_run())
    gap_events = [e for e in collected if isinstance(e, GapEvent)]
    assert len(gap_events) == 0, "No GapEvent expected without overflow"


def test_strategy_on_gap_dispatched() -> None:
    """BaseStrategy.handle_event must dispatch GapEvent to on_gap."""
    strat = _GapAwareStrategy()
    ctx = StrategyContext(
        positions={},
        strategy_id="test-gap",
        intent_factory=lambda **kw: None,
        price_scaler=lambda s, p: int(p),
    )
    ge = GapEvent(missed_count=5, first_missed_seq=100, last_missed_seq=104, ts=999)
    intents = strat.handle_event(ctx, ge)

    assert len(strat.gap_events) == 1
    assert strat.gap_events[0] is ge
    assert intents == []  # on_gap default generates no intents


def test_base_strategy_on_gap_default_noop() -> None:
    """Default BaseStrategy.on_gap is a no-op and does not raise."""
    strat = BaseStrategy.__new__(BaseStrategy)
    strat.strategy_id = "noop"
    strat.config = {}
    strat.symbols = set()
    strat.enabled = True
    strat.ctx = None
    strat._generated_intents = []

    ge = GapEvent(missed_count=1, first_missed_seq=0, last_missed_seq=0, ts=1)
    # Should not raise
    strat.on_gap(ge)
    # And handle_event should also not raise
    ctx = StrategyContext(
        positions={},
        strategy_id="noop",
        intent_factory=lambda **kw: None,
        price_scaler=lambda s, p: int(p),
    )
    intents = strat.handle_event(ctx, ge)
    assert intents == []
