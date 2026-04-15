"""Tests for GapEvent injection on RingBufferBus overflow and strategy dispatch."""

import asyncio

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.events import GapEvent
from hft_platform.strategies.r47_maker import R47MakerStrategy
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


def test_r47_on_gap_preserves_pending_counters() -> None:
    """R47 on_gap must NOT clear _pending_buy/_pending_sell.

    Clearing pending counters after a gap resets the max_pos protection,
    allowing the strategy to send unbounded orders. If gap swallows
    fill/cancel callbacks, keeping pending non-zero is the safe failure
    mode — strategy stops quoting until restart (liveness issue, not
    safety issue). Clearing pending is the UNSAFE failure mode that
    caused the 76-order burst incident (2026-04-15 RC-1).
    """
    strat = R47MakerStrategy(strategy_id="test-r47", symbols=["TXFD6"])
    # Simulate pending orders
    strat._pending_buy["TXFD6"] = 2
    strat._pending_sell["TXFD6"] = 1
    strat._last_bid["TXFD6"] = 1000000
    strat._last_ask["TXFD6"] = 1001000

    gap = GapEvent(missed_count=50, first_missed_seq=10, last_missed_seq=59, ts=123)
    strat.on_gap(gap)

    # Pending counters must be PRESERVED — not cleared
    assert strat._pending_buy["TXFD6"] == 2, "pending_buy must not be cleared by on_gap"
    assert strat._pending_sell["TXFD6"] == 1, "pending_sell must not be cleared by on_gap"
    # Stale streaming state (prices, features) should still be cleared
    assert len(strat._last_bid) == 0, "_last_bid not cleared"
    assert len(strat._last_ask) == 0, "_last_ask not cleared"
    assert len(strat._feature_cache) == 0, "feature_cache not cleared"


def test_r47_max_pos_not_bypassed_after_gap() -> None:
    """After on_gap, max_pos gate must still block orders when pending is non-zero.

    Regression test for 2026-04-15 incident: GapEvent cleared pending
    counters, causing pos(0) + pending(0) < max_pos(1) to pass on every
    tick, sending 76 orders to the broker.
    """
    strat = R47MakerStrategy(
        strategy_id="test-r47", symbols=["TMFD6"], max_pos=1,
    )
    # Simulate: 1 pending buy already sent to broker
    strat._pending_buy["TMFD6"] = 1
    strat._local_pos["TMFD6"] = 0

    # GapEvent fires (bus overflow)
    gap = GapEvent(missed_count=10, first_missed_seq=0, last_missed_seq=9, ts=123)
    strat.on_gap(gap)

    # After gap, pending must still be 1 — preventing the buy gate from
    # passing (0 + 1 < 1 → False). If pending was cleared, the gate
    # would pass (0 + 0 < 1 → True), sending a duplicate order.
    pos = strat._local_pos.get("TMFD6", 0)
    pending = strat._pending_buy.get("TMFD6", 0)
    assert pos + pending >= strat._max_pos, (
        f"max_pos bypass: pos({pos}) + pending({pending}) < max_pos({strat._max_pos})"
    )


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


def test_r47_local_pos_hard_cap_blocks_order_after_gap() -> None:
    """F1: _local_pos hard cap prevents orders even when pending is zero.

    Regression test for 76-order burst (2026-04-15). Even if on_gap
    somehow cleared pending counters (old behavior) or pending drifted to 0,
    _local_pos at max_pos must block further orders.
    """
    strat = R47MakerStrategy(
        strategy_id="test-r47", symbols=["TMFD6"], max_pos=1,
    )
    # Simulate: position is already at max
    strat._local_pos["TMFD6"] = 1
    strat._pending_buy["TMFD6"] = 0  # pending reset (e.g. by bug)

    # pos=1, max_pos=1 → pos < max_pos is False → can_buy must be False
    pos = strat._local_pos["TMFD6"]
    assert pos >= strat._max_pos, "hard cap must block when pos >= max_pos"


def test_r47_risk_feedback_preserves_last_price() -> None:
    """F3: on_risk_feedback must NOT clear _last_bid/_last_ask.

    Clearing price gate on rejection creates a reject→resend amplification
    loop (76-order burst incident 2026-04-15 RC-2).
    """
    from hft_platform.contracts.strategy import Side

    strat = R47MakerStrategy(
        strategy_id="test-r47", symbols=["TMFD6"], max_pos=1,
    )
    strat._pending_buy["TMFD6"] = 1
    strat._last_bid["TMFD6"] = 367250000
    strat._last_ask["TMFD6"] = 367280000

    class _MockFeedback:
        symbol = "TMFD6"
        side = Side.BUY
        reason_code = "DEGRADE"
        was_approved = False

    strat.on_risk_feedback(_MockFeedback())

    # Pending should be decremented
    assert strat._pending_buy["TMFD6"] == 0
    # But last_bid must NOT be cleared — price gate must stay armed
    assert strat._last_bid.get("TMFD6") == 367250000, (
        "_last_bid must not be cleared by risk rejection"
    )
    # last_ask untouched (rejection was BUY side)
    assert strat._last_ask.get("TMFD6") == 367280000


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
