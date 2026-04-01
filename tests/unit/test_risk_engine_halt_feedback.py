"""Tests for C1 fix: HALT post-approve block must send RiskFeedback to rejection_sink.

When StormGuard transitions to HALT between evaluate() approving an intent and
the post-evaluation HALT check, the approved command was previously silently dropped.
This test suite verifies:
- RiskFeedback is sent to _rejection_sink with reason HALT_BLOCKED_POST_APPROVE
- risk_halt_blocked_total metric is incremented
- Safety orders (CANCEL, FORCE_FLAT) are NOT blocked and reach order_queue

The race condition under test:
  1. evaluate() calls storm_guard.validate() → returns (True, "OK") [NORMAL state]
  2. StormGuard transitions to HALT (external trigger, different thread)
  3. run() loop checks storm_guard.state → HALT
  4. Command is blocked → must send RiskFeedback (the C1 fix)
"""

from __future__ import annotations

import asyncio

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderIntent,
    RiskFeedback,
    Side,
    StormGuardState,
)
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


def _make_intent(
    intent_type: IntentType,
    intent_id: int = 42,
    strategy_id: str = "strat1",
    symbol: str = "2330",
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=Side.BUY,
        price=1000000,  # scaled x10000
        qty=1,
        tif=TIF.ROD,
        idempotency_key=None,
        ttl_ns=0,
    )


class _RaceHaltStormGuard(StormGuard):
    """
    A StormGuard subclass that simulates the post-approve HALT race condition:
    - validate() always returns (True, "OK") — as if state was NORMAL at evaluate() time
    - state is set to HALT — so the post-approve dispatch check sees HALT

    This models the real race where StormGuard transitions to HALT between
    evaluate() returning approved and the command dispatch check in run().
    """

    def validate(self, intent: OrderIntent) -> tuple[bool, str]:  # type: ignore[override]
        # Simulate: StormGuard was NORMAL when evaluate() ran → approved
        return (True, "OK")


def _make_engine_with_race_halt(risk_config: str, rejection_sink: asyncio.Queue | None = None) -> RiskEngine:
    """
    Build a RiskEngine that exercises the post-approve HALT race condition path.
    validate() says OK (NORMAL), but storm_guard.state is HALT at dispatch check.
    """
    storm_guard = _RaceHaltStormGuard()
    # Set state to HALT so the post-approve check in run() blocks the command
    storm_guard.state = StormGuardState.HALT

    q_in: asyncio.Queue[OrderIntent] = asyncio.Queue()
    q_out: asyncio.Queue = asyncio.Queue()

    engine = RiskEngine(risk_config, q_in, q_out, storm_guard=storm_guard)
    if rejection_sink is not None:
        engine._rejection_sink = rejection_sink
    return engine


@pytest.mark.asyncio
async def test_halt_blocked_sends_risk_feedback(risk_config):
    """When HALT blocks a post-approved command, RiskFeedback is sent to _rejection_sink."""
    rejection_sink: asyncio.Queue[RiskFeedback] = asyncio.Queue()
    engine = _make_engine_with_race_halt(risk_config, rejection_sink)

    intent = _make_intent(IntentType.NEW, intent_id=42, strategy_id="strat1", symbol="2330")
    engine.intent_queue.put_nowait(intent)

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)

    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Order queue must be empty — command was blocked
    assert engine.order_queue.empty(), "NEW intent should be blocked during post-approve HALT"

    # Rejection sink must have received feedback
    assert not rejection_sink.empty(), "RiskFeedback must be sent when HALT blocks post-approved intent"
    feedback: RiskFeedback = rejection_sink.get_nowait()

    assert feedback.reason_code == "HALT_BLOCKED_POST_APPROVE"
    assert feedback.strategy_id == "strat1"
    assert feedback.symbol == "2330"
    assert feedback.intent_id == 42
    assert feedback.timestamp_ns > 0


@pytest.mark.asyncio
async def test_halt_blocked_increments_metric(risk_config):
    """risk_halt_blocked_total counter must still be incremented when HALT blocks intent."""
    engine = _make_engine_with_race_halt(risk_config)

    # Capture initial counter value via the prometheus client internal API
    initial_count = engine.metrics.risk_halt_blocked_total._value.get()  # type: ignore[attr-defined]

    intent = _make_intent(IntentType.NEW)
    engine.intent_queue.put_nowait(intent)

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)

    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    final_count = engine.metrics.risk_halt_blocked_total._value.get()  # type: ignore[attr-defined]
    assert final_count > initial_count, "risk_halt_blocked_total must be incremented when HALT blocks"


@pytest.mark.asyncio
async def test_cancel_not_blocked_by_halt_sends_no_feedback(risk_config):
    """CANCEL safety orders must pass through HALT and NOT send rejection feedback."""
    storm_guard = StormGuard()
    storm_guard.trigger_halt("test_halt")

    q_in: asyncio.Queue[OrderIntent] = asyncio.Queue()
    q_out: asyncio.Queue = asyncio.Queue()
    rejection_sink: asyncio.Queue[RiskFeedback] = asyncio.Queue()

    engine = RiskEngine(risk_config, q_in, q_out, storm_guard=storm_guard)
    engine._rejection_sink = rejection_sink

    intent = _make_intent(IntentType.CANCEL, intent_id=99)
    q_in.put_nowait(intent)

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)

    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Safety order must reach order_queue
    assert not q_out.empty(), "CANCEL must pass through HALT to order_queue"
    cmd = q_out.get_nowait()
    assert cmd.intent.intent_type == IntentType.CANCEL

    # No feedback for safety orders
    assert rejection_sink.empty(), "CANCEL must not send rejection feedback"


@pytest.mark.asyncio
async def test_force_flat_not_blocked_by_halt_sends_no_feedback(risk_config):
    """FORCE_FLAT safety orders must pass through HALT and NOT send rejection feedback."""
    storm_guard = StormGuard()
    storm_guard.trigger_halt("test_halt")

    q_in: asyncio.Queue[OrderIntent] = asyncio.Queue()
    q_out: asyncio.Queue = asyncio.Queue()
    rejection_sink: asyncio.Queue[RiskFeedback] = asyncio.Queue()

    engine = RiskEngine(risk_config, q_in, q_out, storm_guard=storm_guard)
    engine._rejection_sink = rejection_sink

    intent = _make_intent(IntentType.FORCE_FLAT, intent_id=77)
    q_in.put_nowait(intent)

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)

    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Safety order must reach order_queue
    assert not q_out.empty(), "FORCE_FLAT must pass through HALT to order_queue"
    cmd = q_out.get_nowait()
    assert cmd.intent.intent_type == IntentType.FORCE_FLAT

    # No feedback for safety orders
    assert rejection_sink.empty(), "FORCE_FLAT must not send rejection feedback"


@pytest.mark.asyncio
async def test_halt_blocked_feedback_reason_code_and_context(risk_config):
    """The reason_code, strategy_id and symbol in RiskFeedback must match the blocked intent."""
    rejection_sink: asyncio.Queue[RiskFeedback] = asyncio.Queue()
    engine = _make_engine_with_race_halt(risk_config, rejection_sink)

    intent = _make_intent(IntentType.NEW, intent_id=1, strategy_id="test_strat", symbol="TXFD6")
    engine.intent_queue.put_nowait(intent)

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)

    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert not rejection_sink.empty(), "RiskFeedback must be present"
    feedback = rejection_sink.get_nowait()
    assert feedback.reason_code == "HALT_BLOCKED_POST_APPROVE"
    assert feedback.strategy_id == "test_strat"
    assert feedback.symbol == "TXFD6"
    assert feedback.intent_id == 1


@pytest.mark.asyncio
async def test_halt_blocked_no_rejection_sink_does_not_raise(risk_config):
    """When _rejection_sink is None, blocking by HALT must not raise exceptions."""
    engine = _make_engine_with_race_halt(risk_config, rejection_sink=None)
    engine._rejection_sink = None  # Ensure None

    intent = _make_intent(IntentType.NEW)
    engine.intent_queue.put_nowait(intent)

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)

    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Verify command was still blocked
    assert engine.order_queue.empty(), "Command must be blocked even without rejection_sink"
