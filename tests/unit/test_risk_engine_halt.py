"""Tests for DECISION-01 fix: CANCEL/FORCE_FLAT must pass through HALT state.

Also verifies INFRA-01: RiskEngine uses injected StormGuard (not a private one).
"""

import asyncio

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side, StormGuardState
from hft_platform.risk.engine import RiskEngine
from hft_platform.risk.storm_guard import StormGuard


@pytest.fixture
def risk_config(tmp_path):
    cfg = tmp_path / "risk.yaml"
    cfg.write_text(
        """
risk:
  max_order_size: 100
  max_position: 200
  max_notional: 10000000
"""
    )
    return str(cfg)


def _make_intent(intent_type: IntentType, intent_id: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="s1",
        symbol="2330",
        intent_type=intent_type,
        side=Side.BUY,
        price=1000000,  # scaled x10000
        qty=1,
        tif=TIF.ROD,
        idempotency_key=None,
        ttl_ns=0,
    )


@pytest.mark.asyncio
async def test_cancel_passes_through_halt(risk_config):
    """CANCEL intent must reach order_queue even when StormGuard is HALT."""
    storm_guard = StormGuard()
    storm_guard.trigger_halt("test_halt")
    assert storm_guard.state == StormGuardState.HALT

    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    engine = RiskEngine(risk_config, q_in, q_out, storm_guard=storm_guard)

    intent = _make_intent(IntentType.CANCEL)
    q_in.put_nowait(intent)

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)

    assert not q_out.empty(), "CANCEL should pass through HALT"
    cmd = q_out.get_nowait()
    assert cmd.intent.intent_type == IntentType.CANCEL

    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_force_flat_passes_through_halt(risk_config):
    """FORCE_FLAT intent must reach order_queue even when StormGuard is HALT."""
    storm_guard = StormGuard()
    storm_guard.trigger_halt("test_halt")
    assert storm_guard.state == StormGuardState.HALT

    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    engine = RiskEngine(risk_config, q_in, q_out, storm_guard=storm_guard)

    intent = _make_intent(IntentType.FORCE_FLAT)
    q_in.put_nowait(intent)

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)

    assert not q_out.empty(), "FORCE_FLAT should pass through HALT"
    cmd = q_out.get_nowait()
    assert cmd.intent.intent_type == IntentType.FORCE_FLAT

    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_new_blocked_during_halt(risk_config):
    """NEW intent must be blocked when StormGuard is HALT."""
    storm_guard = StormGuard()
    storm_guard.trigger_halt("test_halt")
    assert storm_guard.state == StormGuardState.HALT

    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    engine = RiskEngine(risk_config, q_in, q_out, storm_guard=storm_guard)

    intent = _make_intent(IntentType.NEW)
    q_in.put_nowait(intent)

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)

    assert q_out.empty(), "NEW intent should be blocked during HALT"

    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_risk_engine_uses_injected_storm_guard(risk_config):
    """RiskEngine must use the injected StormGuard, not create a private one."""
    shared_guard = StormGuard()
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    engine = RiskEngine(risk_config, q_in, q_out, storm_guard=shared_guard)

    assert engine.storm_guard is shared_guard, "RiskEngine should use injected StormGuard instance"


def test_risk_engine_creates_default_storm_guard_when_none(risk_config):
    """RiskEngine creates its own StormGuard when none is injected."""
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    engine = RiskEngine(risk_config, q_in, q_out)

    assert engine.storm_guard is not None
    assert isinstance(engine.storm_guard, StormGuard)
