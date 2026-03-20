"""Expanded tests for StrategyRunner event dispatch, circuit breaker, and lifecycle."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.events import (
    BidAskEvent,
    FeatureUpdateEvent,
    LOBStatsEvent,
    TickEvent,
)
from hft_platform.strategy.base import BaseStrategy
from hft_platform.strategy.runner import StrategyRunner
from tests.conftest import make_bidask_event, make_tick_event
from tests.factories.events import make_lob_stats_event

# ---------------------------------------------------------------------------
# Helper strategies
# ---------------------------------------------------------------------------


class RecordingStrategy(BaseStrategy):
    """Records every event it receives and optionally generates intents."""

    def __init__(self, strategy_id: str, symbols=None, generate_intent: bool = False):
        super().__init__(strategy_id=strategy_id, symbols=symbols or [])
        self.events_received: list = []
        self._generate_intent = generate_intent

    def on_tick(self, event: TickEvent) -> None:
        self.events_received.append(("tick", event))
        if self._generate_intent:
            self.buy(event.symbol, 1_000_000, 1)

    def on_book_update(self, event: BidAskEvent) -> None:
        self.events_received.append(("bidask", event))
        if self._generate_intent:
            self.buy(event.symbol, 1_000_000, 1)

    def on_stats(self, event: LOBStatsEvent) -> None:
        self.events_received.append(("lob_stats", event))
        if self._generate_intent:
            self.buy(event.symbol, 1_000_000, 1)

    def on_features(self, event: FeatureUpdateEvent) -> None:
        self.events_received.append(("feature", event))
        if self._generate_intent:
            self.buy(event.symbol, 1_000_000, 1)


class ExplodingStrategy(BaseStrategy):
    """Always raises an exception in handle_event."""

    def __init__(self, strategy_id: str, symbols=None):
        super().__init__(strategy_id=strategy_id, symbols=symbols or [])

    def on_tick(self, event: TickEvent) -> None:
        raise RuntimeError("boom")

    def on_book_update(self, event: BidAskEvent) -> None:
        raise RuntimeError("boom")

    def on_stats(self, event: LOBStatsEvent) -> None:
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_rust_circuit(monkeypatch):
    """Disable Rust circuit breaker so Python FSM is exercised."""
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_RUST", "0")
    monkeypatch.setenv("HFT_TYPED_INTENT_CHANNEL", "0")
    monkeypatch.setenv("HFT_STRICT_PRICE_MODE", "0")
    monkeypatch.setenv("HFT_STRATEGY_FEATURE_COMPAT_FAIL_FAST", "0")


def _make_runner() -> tuple[StrategyRunner, asyncio.Queue]:
    bus = MagicMock()
    risk_queue = asyncio.Queue()
    with (
        patch("hft_platform.strategy.runner.StrategyRegistry") as MockReg,
        patch("hft_platform.strategy.runner.MetricsRegistry") as MockMetrics,
        patch("hft_platform.strategy.runner.LatencyRecorder") as MockLatency,
    ):
        MockReg.return_value.instantiate.return_value = []
        MockMetrics.get.return_value = None
        MockLatency.get.return_value = None
        runner = StrategyRunner(bus, risk_queue, config_path="dummy")
    return runner, risk_queue


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_event_dispatched_to_strategy():
    """TickEvent is routed to strategy via on_tick."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("s1", symbols=["2330"])
    runner.register(strat)

    event = make_tick_event(symbol="2330")
    await runner.process_event(event)

    assert len(strat.events_received) == 1
    assert strat.events_received[0][0] == "tick"
    assert strat.events_received[0][1] is event


@pytest.mark.asyncio
async def test_bidask_event_dispatched():
    """BidAskEvent is routed to strategy via on_book_update."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("s1", symbols=["2330"])
    runner.register(strat)

    event = make_bidask_event(symbol="2330")
    await runner.process_event(event)

    assert len(strat.events_received) == 1
    assert strat.events_received[0][0] == "bidask"
    assert strat.events_received[0][1] is event


@pytest.mark.asyncio
async def test_lob_stats_event_dispatched():
    """LOBStatsEvent is routed to strategy via on_stats."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("s1", symbols=["2330"])
    runner.register(strat)

    event = make_lob_stats_event(symbol="2330")
    await runner.process_event(event)

    assert len(strat.events_received) == 1
    assert strat.events_received[0][0] == "lob_stats"
    assert strat.events_received[0][1] is event


@pytest.mark.asyncio
async def test_feature_update_event_dispatched():
    """FeatureUpdateEvent is routed to strategy via on_features."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("s1", symbols=["2330"])
    runner.register(strat)

    event = FeatureUpdateEvent(
        symbol="2330",
        ts=1_000_000,
        local_ts=1_000_001,
        seq=1,
        feature_set_id="default",
        schema_version=1,
        changed_mask=0xFF,
        warmup_ready_mask=0xFF,
        quality_flags=0,
        feature_ids=("ofi_l1",),
        values=(42,),
    )
    await runner.process_event(event)

    assert len(strat.events_received) == 1
    assert strat.events_received[0][0] == "feature"
    assert strat.events_received[0][1] is event


@pytest.mark.asyncio
async def test_strategy_exception_isolation():
    """Exception in one strategy does not prevent other strategies from executing."""
    runner, _rq = _make_runner()
    exploder = ExplodingStrategy("bad", symbols=["2330"])
    good = RecordingStrategy("good", symbols=["2330"])
    runner.register(exploder)
    runner.register(good)

    event = make_tick_event(symbol="2330")
    await runner.process_event(event)

    # good strategy still received the event
    assert len(good.events_received) == 1
    assert good.events_received[0][0] == "tick"


@pytest.mark.asyncio
async def test_strategy_exception_returns_empty_intents():
    """After exception, intents list is empty — nothing submitted to risk_queue."""
    runner, rq = _make_runner()
    exploder = ExplodingStrategy("bad", symbols=["2330"])
    runner.register(exploder)

    event = make_tick_event(symbol="2330")
    await runner.process_event(event)

    assert rq.qsize() == 0


@pytest.mark.asyncio
async def test_intents_submitted_to_risk_queue():
    """Strategy-generated intents are forwarded to the risk queue."""
    runner, rq = _make_runner()
    strat = RecordingStrategy("s1", symbols=["2330"], generate_intent=True)
    runner.register(strat)

    event = make_tick_event(symbol="2330")
    await runner.process_event(event)

    assert rq.qsize() == 1
    intent = rq.get_nowait()
    assert isinstance(intent, OrderIntent)
    assert intent.strategy_id == "s1"
    assert intent.symbol == "2330"


@pytest.mark.asyncio
async def test_multiple_strategies_all_receive_event():
    """Two strategies subscribed to the same symbol both receive the event."""
    runner, _rq = _make_runner()
    s1 = RecordingStrategy("s1", symbols=["2330"])
    s2 = RecordingStrategy("s2", symbols=["2330"])
    runner.register(s1)
    runner.register(s2)

    event = make_tick_event(symbol="2330")
    await runner.process_event(event)

    assert len(s1.events_received) == 1
    assert len(s2.events_received) == 1


@pytest.mark.asyncio
async def test_disabled_strategy_skipped():
    """Strategy with enabled=False does not receive events."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("s1", symbols=["2330"])
    runner.register(strat)
    strat.enabled = False

    event = make_tick_event(symbol="2330")
    await runner.process_event(event)

    assert len(strat.events_received) == 0


def test_registration_builds_executor_cache():
    """register() populates _strat_executors with a tuple entry for the strategy."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("s1")
    assert len(runner._strat_executors) == 0

    runner.register(strat)

    assert len(runner._strat_executors) == 1
    assert runner._strat_executors[0][0] is strat


@pytest.mark.asyncio
async def test_symbol_filtering():
    """Strategy subscribed to '2330' does not receive events for '2317'."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("s1", symbols=["2330"])
    runner.register(strat)

    event = make_tick_event(symbol="2317")
    await runner.process_event(event)

    assert len(strat.events_received) == 0


@pytest.mark.asyncio
async def test_circuit_breaker_degrades_on_failures(monkeypatch):
    """After threshold/2 failures the circuit state transitions to 'degraded'."""
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_THRESHOLD", "10")
    runner, _rq = _make_runner()
    exploder = ExplodingStrategy("bad", symbols=["2330"])
    runner.register(exploder)

    # half_threshold = 10 // 2 = 5
    for _ in range(5):
        event = make_tick_event(symbol="2330")
        await runner.process_event(event)

    assert runner._circuit_states.get("bad") == "degraded"
    # Strategy should still be enabled in degraded state
    assert exploder.enabled is True


@pytest.mark.asyncio
async def test_circuit_breaker_halts_on_threshold(monkeypatch):
    """After threshold failures the circuit state transitions to 'halted' and strategy is disabled."""
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_THRESHOLD", "6")
    runner, _rq = _make_runner()
    exploder = ExplodingStrategy("bad", symbols=["2330"])
    runner.register(exploder)

    for _ in range(6):
        event = make_tick_event(symbol="2330")
        await runner.process_event(event)

    assert runner._circuit_states.get("bad") == "halted"
    assert exploder.enabled is False


@pytest.mark.asyncio
async def test_circuit_breaker_recovery(monkeypatch):
    """After recovery_threshold consecutive successes in degraded state, circuit recovers to normal."""
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_THRESHOLD", "6")
    runner, _rq = _make_runner()

    # Use a strategy that fails only a controlled number of times
    class FailNTimes(BaseStrategy):
        def __init__(self, n: int):
            super().__init__(strategy_id="flakey", symbols=["2330"])
            self._fail_count = n
            self._calls = 0

        def on_tick(self, event: TickEvent) -> None:
            self._calls += 1
            if self._calls <= self._fail_count:
                raise RuntimeError("oops")

    # half_threshold = 3, so 3 failures → degraded
    strat = FailNTimes(n=3)
    runner.register(strat)

    # 3 failures → degraded
    for _ in range(3):
        await runner.process_event(make_tick_event(symbol="2330"))

    assert runner._circuit_states.get("flakey") == "degraded"

    # recovery_threshold = 6 // 2 = 3 consecutive successes needed
    for _ in range(3):
        await runner.process_event(make_tick_event(symbol="2330"))

    assert runner._circuit_states.get("flakey") == "normal"
    assert runner._failure_counts.get("flakey") == 0


@pytest.mark.asyncio
async def test_process_event_updates_positions_cache():
    """When positions_dirty is True, positions cache is rebuilt before dispatch."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("s1", symbols=["2330"])
    runner.register(strat)

    # Set dirty flag
    runner._positions_dirty = True

    # Create a mock position store
    mock_store = MagicMock()
    mock_store.positions = {"pos:s1:2330": MagicMock(net_qty=10)}
    mock_store.positions["pos:s1:2330"].strategy_id = None  # no strategy_id attr
    runner.position_store = mock_store

    await runner.process_event(make_tick_event(symbol="2330"))

    # After processing, dirty flag should be cleared
    assert runner._positions_dirty is False


@pytest.mark.asyncio
async def test_run_lifecycle_start_stop():
    """run() sets running=True, cancellation sets running to cleanup state."""
    runner, _rq = _make_runner()

    # Mock bus.consume to yield nothing then cancel
    async def _empty_consume():
        return
        yield  # noqa: unreachable — makes this an async generator

    runner.bus.consume = _empty_consume

    assert runner.running is False

    task = asyncio.ensure_future(runner.run())
    await asyncio.sleep(0.01)

    assert runner.running is True

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_intent_factory_creates_order_intent():
    """_intent_factory returns a valid OrderIntent when typed channel is off."""
    runner, _rq = _make_runner()

    intent = runner._intent_factory(
        strategy_id="s1",
        symbol="2330",
        side=Side.BUY,
        price=1_000_000,
        qty=5,
        tif=TIF.LIMIT,
        intent_type=IntentType.NEW,
    )

    assert isinstance(intent, OrderIntent)
    assert intent.strategy_id == "s1"
    assert intent.symbol == "2330"
    assert intent.side == Side.BUY
    assert intent.price == 1_000_000
    assert intent.qty == 5
    assert intent.intent_type == IntentType.NEW
    assert intent.intent_id == 1


def test_duplicate_strategy_registration():
    """Registering the same strategy twice adds it twice to the list."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("dup")
    runner.register(strat)
    runner.register(strat)

    assert len(runner.strategies) == 2
    assert len(runner._strat_executors) == 2
    assert runner._strat_index["dup"] == [0, 1]


def test_extract_event_trace_from_meta():
    """_extract_event_trace with MetaData returns (local_ts, 'topic:seq')."""
    from hft_platform.events import MetaData

    runner, _rq = _make_runner()
    event = make_tick_event(
        symbol="2330",
        meta=MetaData(seq=42, topic="tick", source_ts=100, local_ts=999),
    )

    source_ts_ns, trace_id = runner._extract_event_trace(event)

    assert source_ts_ns == 999
    assert trace_id == "tick:42"


def test_extract_event_trace_no_meta():
    """_extract_event_trace with plain object (no meta) falls back gracefully."""
    runner, _rq = _make_runner()

    class PlainEvent:
        symbol = "2330"

    event = PlainEvent()
    source_ts_ns, trace_id = runner._extract_event_trace(event)

    # Falls back to timebase.now_ns() since no meta and no ts attr
    assert source_ts_ns > 0
    assert trace_id == ""


@pytest.mark.asyncio
async def test_symbol_filtering_allows_subscribed_symbol():
    """Strategy subscribed to '2330' receives events for '2330'."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("s1", symbols=["2330"])
    runner.register(strat)

    event = make_tick_event(symbol="2330")
    await runner.process_event(event)

    assert len(strat.events_received) == 1


@pytest.mark.asyncio
async def test_no_symbol_filter_receives_all():
    """Strategy with empty symbols set receives all events."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("s1", symbols=[])
    runner.register(strat)

    event = make_tick_event(symbol="9999")
    await runner.process_event(event)

    assert len(strat.events_received) == 1


@pytest.mark.asyncio
async def test_multiple_intents_all_submitted():
    """All intents returned by multiple strategies are submitted to risk queue."""
    runner, rq = _make_runner()
    s1 = RecordingStrategy("s1", symbols=["2330"], generate_intent=True)
    s2 = RecordingStrategy("s2", symbols=["2330"], generate_intent=True)
    runner.register(s1)
    runner.register(s2)

    await runner.process_event(make_tick_event(symbol="2330"))

    assert rq.qsize() == 2


@pytest.mark.asyncio
async def test_lob_stats_symbol_filtering():
    """LOBStatsEvent for unsubscribed symbol is filtered out."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("s1", symbols=["2330"])
    runner.register(strat)

    event = make_lob_stats_event(symbol="2317")
    await runner.process_event(event)

    assert len(strat.events_received) == 0


def test_resolve_risk_submit_uses_submit_nowait():
    """If risk_queue has submit_nowait, it is preferred over put_nowait."""
    runner, _rq = _make_runner()

    mock_rq = MagicMock()
    mock_rq.submit_nowait = MagicMock()
    result = runner._resolve_risk_submit(mock_rq)

    assert result is mock_rq.submit_nowait


def test_resolve_risk_submit_falls_back_to_put_nowait():
    """If risk_queue lacks submit_nowait, put_nowait is used."""
    runner, _rq = _make_runner()

    mock_rq = MagicMock(spec=["put_nowait"])
    result = runner._resolve_risk_submit(mock_rq)

    assert result is mock_rq.put_nowait


@pytest.mark.asyncio
async def test_invalidate_positions_sets_dirty_flag():
    """invalidate_positions() sets _positions_dirty to True."""
    runner, _rq = _make_runner()
    runner._positions_dirty = False

    runner.invalidate_positions()

    assert runner._positions_dirty is True


@pytest.mark.asyncio
async def test_delta_source_event_triggers_position_rebuild():
    """Event with delta_source attribute triggers position cache rebuild."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("s1")
    runner.register(strat)

    runner._positions_dirty = False

    class DeltaEvent:
        symbol = "2330"
        delta_source = "fill"

    await runner.process_event(DeltaEvent())

    # delta_source sets dirty, then positions are rebuilt and dirty is cleared
    assert runner._positions_dirty is False


def test_strat_index_populated_on_register():
    """_strat_index maps strategy_id to executor indices."""
    runner, _rq = _make_runner()
    s1 = RecordingStrategy("alpha")
    s2 = RecordingStrategy("beta")
    runner.register(s1)
    runner.register(s2)

    assert "alpha" in runner._strat_index
    assert "beta" in runner._strat_index
    assert runner._strat_index["alpha"] == [0]
    assert runner._strat_index["beta"] == [1]


def test_extract_event_trace_ts_fallback():
    """_extract_event_trace uses event.ts when meta is absent."""
    runner, _rq = _make_runner()

    class TsEvent:
        ts = 5_000_000

    source_ts_ns, trace_id = runner._extract_event_trace(TsEvent())

    assert source_ts_ns == 5_000_000
    assert trace_id == ""


# ---------------------------------------------------------------------------
# Additional coverage: cross-event-type routing, isolation, and enrichment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exception_isolation_on_bidask():
    """Exception in on_book_update does not prevent other strategies from executing."""
    runner, _rq = _make_runner()
    bad = ExplodingStrategy("bad", symbols=["2330"])
    good = RecordingStrategy("good", symbols=["2330"])
    runner.register(bad)
    runner.register(good)

    await runner.process_event(make_bidask_event(symbol="2330"))

    assert len(good.events_received) == 1
    assert good.events_received[0][0] == "bidask"


@pytest.mark.asyncio
async def test_exception_isolation_on_lobstats():
    """Exception in on_stats does not prevent other strategies from executing."""
    runner, _rq = _make_runner()
    bad = ExplodingStrategy("bad", symbols=["2330"])
    good = RecordingStrategy("good", symbols=["2330"])
    runner.register(bad)
    runner.register(good)

    await runner.process_event(make_lob_stats_event(symbol="2330"))

    assert len(good.events_received) == 1
    assert good.events_received[0][0] == "lob_stats"


@pytest.mark.asyncio
async def test_exception_isolation_multiple_bad_strategies():
    """Multiple bad strategies do not prevent a good strategy from executing."""
    runner, _rq = _make_runner()
    bad1 = ExplodingStrategy("bad1", symbols=["2330"])
    bad2 = ExplodingStrategy("bad2", symbols=["2330"])
    good = RecordingStrategy("good", symbols=["2330"])
    runner.register(bad1)
    runner.register(good)
    runner.register(bad2)

    await runner.process_event(make_tick_event(symbol="2330"))

    assert len(good.events_received) == 1


@pytest.mark.asyncio
async def test_bidask_broadcast_to_all_subscribed():
    """BidAskEvent is broadcast to all strategies subscribed to the symbol."""
    runner, _rq = _make_runner()
    a = RecordingStrategy("a", symbols=["2330"])
    b = RecordingStrategy("b", symbols=["2330"])
    runner.register(a)
    runner.register(b)

    await runner.process_event(make_bidask_event(symbol="2330"))

    assert len(a.events_received) == 1
    assert len(b.events_received) == 1
    assert a.events_received[0][0] == "bidask"


@pytest.mark.asyncio
async def test_symbol_filtering_bidask():
    """BidAskEvent for unsubscribed symbol is filtered out."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("s1", symbols=["2330"])
    runner.register(strat)

    await runner.process_event(make_bidask_event(symbol="2454"))

    assert len(strat.events_received) == 0


@pytest.mark.asyncio
async def test_feature_update_symbol_filtering():
    """FeatureUpdateEvent for unsubscribed symbol is filtered out."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("s1", symbols=["2454"])
    runner.register(strat)

    event = FeatureUpdateEvent(
        symbol="2330",
        ts=1_000_000,
        local_ts=1_000_001,
        seq=1,
        feature_set_id="default",
        schema_version=1,
        changed_mask=0xFF,
        warmup_ready_mask=0xFF,
        quality_flags=0,
        feature_ids=("ofi_l1",),
        values=(42,),
    )
    await runner.process_event(event)

    assert len(strat.events_received) == 0


@pytest.mark.asyncio
async def test_process_event_with_no_strategies():
    """process_event with no strategies registered should not crash."""
    runner, _rq = _make_runner()
    assert len(runner.strategies) == 0

    await runner.process_event(make_tick_event(symbol="2330"))
    await runner.process_event(make_bidask_event(symbol="2330"))
    await runner.process_event(make_lob_stats_event(symbol="2330"))


@pytest.mark.asyncio
async def test_multiple_event_types_interleaved():
    """Process a sequence of different event types without issues."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("s1", symbols=["2330"])
    runner.register(strat)

    await runner.process_event(make_tick_event(symbol="2330"))
    await runner.process_event(make_bidask_event(symbol="2330"))
    await runner.process_event(make_lob_stats_event(symbol="2330"))
    await runner.process_event(make_tick_event(symbol="2330"))

    types = [e[0] for e in strat.events_received]
    assert types == ["tick", "bidask", "lob_stats", "tick"]


@pytest.mark.asyncio
async def test_multi_symbol_strategy():
    """Strategy subscribing to multiple symbols receives events for each."""
    runner, _rq = _make_runner()
    strat = RecordingStrategy("multi", symbols=["2330", "2454"])
    runner.register(strat)

    await runner.process_event(make_tick_event(symbol="2330"))
    await runner.process_event(make_tick_event(symbol="2454"))
    await runner.process_event(make_tick_event(symbol="XXXX"))

    assert len(strat.events_received) == 2


@pytest.mark.asyncio
async def test_run_lifecycle_processes_events():
    """run() processes events from bus.consume and sets running flag."""
    bus = MagicMock()
    risk_queue = asyncio.Queue()
    events_to_yield = [
        make_tick_event(symbol="2330"),
        make_tick_event(symbol="2330"),
        make_tick_event(symbol="2330"),
    ]

    async def mock_consume():
        for ev in events_to_yield:
            yield ev

    bus.consume = mock_consume

    with (
        patch("hft_platform.strategy.runner.StrategyRegistry") as MockReg,
        patch("hft_platform.strategy.runner.MetricsRegistry") as MockMetrics,
        patch("hft_platform.strategy.runner.LatencyRecorder") as MockLatency,
    ):
        MockReg.return_value.instantiate.return_value = []
        MockMetrics.get.return_value = None
        MockLatency.get.return_value = None
        runner = StrategyRunner(bus, risk_queue, config_path="dummy")

    strat = RecordingStrategy("s1", symbols=["2330"])
    runner.register(strat)

    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(strat.events_received) == 3
    assert runner.running is True


@pytest.mark.asyncio
async def test_intent_enrichment_from_bidask_event():
    """OrderIntent created from BidAskEvent carries correct source_ts_ns and trace_id."""
    from hft_platform.events import MetaData

    runner, rq = _make_runner()
    strat = RecordingStrategy("s1", symbols=["2330"], generate_intent=True)
    runner.register(strat)

    event = make_bidask_event(
        symbol="2330",
        meta=MetaData(seq=42, topic="bidask", source_ts=1, local_ts=987654321),
    )
    await runner.process_event(event)

    assert rq.qsize() == 1
    intent = rq.get_nowait()
    assert intent.source_ts_ns == 987654321
    assert intent.trace_id == "bidask:42"


@pytest.mark.asyncio
async def test_intent_enrichment_from_lobstats_event():
    """OrderIntent created from LOBStatsEvent uses event.ts as source timestamp."""
    runner, rq = _make_runner()

    class BuyOnStats(BaseStrategy):
        def __init__(self):
            super().__init__(strategy_id="stats_buyer", symbols=["2330"])

        def on_stats(self, event: LOBStatsEvent) -> None:
            self.buy(event.symbol, event.best_bid, 1)

    strat = BuyOnStats()
    runner.register(strat)

    event = make_lob_stats_event(symbol="2330", ts=111222333)
    await runner.process_event(event)

    assert rq.qsize() == 1
    intent = rq.get_nowait()
    assert intent.source_ts_ns == 111222333
