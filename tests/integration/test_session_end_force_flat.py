"""Slice B Task 13 — DoD-B4 integration: session-end FORCE_FLAT residual close-out.

Covers the full StrategyRunner → MakerStrategyBridge.on_session_end →
risk_queue submission path on a SessionPhase.CLOSE_ONLY → FORCE_FLAT
transition.

Fidelity note (lower-fidelity fallback per task spec):
    Driving the full SessionGovernor wall-clock scheduler end-to-end
    requires `services/system.py` orchestration not modified in Task 13
    (the plan explicitly states services/system.py is NOT the wiring
    point). We instead exercise the consumer-side hook
    StrategyRunner.dispatch_session_end_force_flat(track_name) directly,
    after manually transitioning the TrackGate phase to FORCE_FLAT — the
    same shape of call SessionGovernor.transition_track triggers via its
    phase-callback list. The intents still flow through:
        bridge.on_session_end(ctx)
            → runner phase-aware intent filter
            → self._risk_submit(intent)
            → risk_queue (asyncio.Queue)
    which is the load-bearing wiring this task enables.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hft_platform.backtest.maker_bridge import MakerStrategyBridge
from hft_platform.contracts.strategy import IntentType, Side
from hft_platform.ops.session_governor import SessionPhase, TrackGate
from hft_platform.strategy.runner import StrategyRunner

SYMBOL = "TMFD6"
TRACK = "futures_day"


def _build_runner_with_track(track_phase: SessionPhase) -> tuple[StrategyRunner, asyncio.Queue, TrackGate]:
    """Build a minimal StrategyRunner suitable for FORCE_FLAT exercising."""
    bus = SimpleNamespace()  # never consumed in this test
    risk_queue: asyncio.Queue = asyncio.Queue()
    runner = StrategyRunner(bus, risk_queue, lob_engine=None, position_store=None)
    runner.strategies = []

    track_gate = TrackGate()
    track_gate.register_symbol(SYMBOL, TRACK)
    track_gate.set_track_phase(TRACK, track_phase)
    runner.track_gate = track_gate
    return runner, risk_queue, track_gate


def _attach_position_store(runner: StrategyRunner, net_qty: int) -> None:
    """Attach a mock position store reporting net_qty for SYMBOL.

    Position store key shape mirrors hft_platform/strategy/runner.py:131-137:
        "<account>:<strategy_id>:<symbol>" → Position(net_qty=...).

    The Position object MUST expose strategy_id, symbol, and net_qty as
    real attributes (not MagicMock auto-attrs) so _build_positions_by_strategy
    routes the entry to the correct strategy bucket via line 955-957.
    """
    pos = SimpleNamespace(strategy_id="maker_test", symbol=SYMBOL, net_qty=net_qty)
    pos_store = MagicMock(spec=[])
    pos_store.positions = {f"acct:maker_test:{SYMBOL}": pos}
    pos_store._rust_tracker = None
    runner.position_store = pos_store


@pytest.mark.asyncio
async def test_force_flat_drains_residual_long_through_risk_queue():
    """End-to-end: long residual → SELL FORCE_FLAT MARKET intent → risk_queue."""
    runner, risk_queue, _ = _build_runner_with_track(SessionPhase.FORCE_FLAT)

    inner = MagicMock()
    bridge = MakerStrategyBridge(inner=inner, strategy_id="maker_test", symbol=SYMBOL)
    runner.register(bridge)
    _attach_position_store(runner, net_qty=1)

    # Trigger the new consumer-side hook (mirrors phase-callback dispatch).
    await runner.dispatch_session_end_force_flat(track_name=TRACK)

    # One FORCE_FLAT intent must reach the risk queue.
    intent = await asyncio.wait_for(risk_queue.get(), timeout=1.0)
    assert intent.intent_type == IntentType.FORCE_FLAT
    assert intent.side == Side.SELL
    assert intent.qty == 1
    assert intent.symbol == SYMBOL
    assert intent.strategy_id == "maker_test"
    assert intent.price_type == "MKT"
    assert intent.reason == "session_end_force_flat"
    # Risk queue should be empty after the single drain.
    assert risk_queue.empty()


@pytest.mark.asyncio
async def test_force_flat_drains_residual_short_through_risk_queue():
    """Short residual → BUY FORCE_FLAT MARKET intent → risk_queue."""
    runner, risk_queue, _ = _build_runner_with_track(SessionPhase.FORCE_FLAT)

    inner = MagicMock()
    bridge = MakerStrategyBridge(inner=inner, strategy_id="maker_test", symbol=SYMBOL)
    runner.register(bridge)
    _attach_position_store(runner, net_qty=-2)

    await runner.dispatch_session_end_force_flat(track_name=TRACK)

    intent = await asyncio.wait_for(risk_queue.get(), timeout=1.0)
    assert intent.intent_type == IntentType.FORCE_FLAT
    assert intent.side == Side.BUY
    assert intent.qty == 2
    assert intent.reason == "session_end_force_flat"


@pytest.mark.asyncio
async def test_force_flat_flat_position_emits_no_intent():
    """Flat residual → no intent emitted, risk_queue stays empty."""
    runner, risk_queue, _ = _build_runner_with_track(SessionPhase.FORCE_FLAT)

    inner = MagicMock()
    bridge = MakerStrategyBridge(inner=inner, strategy_id="maker_test", symbol=SYMBOL)
    runner.register(bridge)
    _attach_position_store(runner, net_qty=0)

    await runner.dispatch_session_end_force_flat(track_name=TRACK)

    await asyncio.sleep(0)  # let any pending coroutine yield
    assert risk_queue.empty()


@pytest.mark.asyncio
async def test_force_flat_passes_phase_filter_for_force_flat_intent():
    """The FORCE_FLAT intent type is permitted under SessionPhase.FORCE_FLAT
    by StrategyRunner.filter_intents_by_phase (runner.py:1623-1625).

    This guards against a regression where a future change to the filter
    accidentally drops the residual close-out intent.
    """
    runner, risk_queue, track_gate = _build_runner_with_track(SessionPhase.FORCE_FLAT)

    inner = MagicMock()
    bridge = MakerStrategyBridge(inner=inner, strategy_id="maker_test", symbol=SYMBOL)
    runner.register(bridge)
    _attach_position_store(runner, net_qty=1)

    await runner.dispatch_session_end_force_flat(track_name=TRACK)

    # Use the same static filter the production runner uses on each event.
    pending = []
    while not risk_queue.empty():
        pending.append(risk_queue.get_nowait())

    filtered = StrategyRunner.filter_intents_by_phase(
        pending, track_gate, runner.position_store, strategy_id="maker_test"
    )
    assert len(filtered) == 1
    assert filtered[0].intent_type == IntentType.FORCE_FLAT


@pytest.mark.asyncio
async def test_non_maker_strategies_ignored_by_session_end_dispatch():
    """Strategies without on_session_end (regular BaseStrategy subclasses)
    must be silently skipped — no AttributeError, no spurious intents."""
    from hft_platform.strategies.simple_mm import SimpleMarketMaker

    runner, risk_queue, _ = _build_runner_with_track(SessionPhase.FORCE_FLAT)
    mm = SimpleMarketMaker(strategy_id="mm-01", subscribe_symbols=[SYMBOL])
    runner.register(mm)
    _attach_position_store(runner, net_qty=0)

    # Should not raise.
    await runner.dispatch_session_end_force_flat(track_name=TRACK)

    await asyncio.sleep(0)
    assert risk_queue.empty()
