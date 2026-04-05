import asyncio
import dataclasses
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.risk.engine import RiskEngine


@pytest.fixture
def engine(tmp_path):
    # Temp config matching structure expected by validators
    # Validators source config using self.config.get(...)
    # Let's assume standard structure
    cfg = tmp_path / "risk.yaml"
    cfg.write_text("""
    risk:
      max_order_size: 10
      max_position: 20
      max_notional: 1000000
    """)
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    return RiskEngine(str(cfg), q_in, q_out)


@pytest.mark.asyncio
async def test_risk_validation_pass(engine):
    intent = OrderIntent(1, "s1", "2330", IntentType.NEW, Side.BUY, 100, 5, TIF.ROD, None, 0)

    # We must enable validators manually if they default to off or rely on complex config?
    # Validators use self.config
    decision = engine.evaluate(intent)
    assert decision.approved


@pytest.mark.asyncio
async def test_risk_lifecycle(engine):
    intent = OrderIntent(3, "s1", "2330", IntentType.NEW, Side.BUY, 100, 1, TIF.ROD, None, 0)

    engine.intent_queue.put_nowait(intent)

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)

    # Valid intent should pass to order_queue
    assert not engine.order_queue.empty()
    out = engine.order_queue.get_nowait()
    assert out.cmd_id > 0
    assert out.intent.intent_id == 3

    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_risk_reject_path_safe_when_metrics_none(engine):
    # PRICE_ZERO_OR_NEG -> reject path; metrics is intentionally disabled to ensure best-effort guard.
    engine.metrics = None
    intent = OrderIntent(4, "s1", "2330", IntentType.NEW, Side.BUY, 0, 1, TIF.ROD, None, 0)
    engine.intent_queue.put_nowait(intent)

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)

    # No command should be emitted on reject.
    assert engine.order_queue.empty()

    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_rust_validator_fail_closed(engine):
    """When Rust validator errors, engine falls through to Python validators."""
    mock_rv = MagicMock()
    mock_rv.check.side_effect = RuntimeError("segfault")
    engine._rust_validator = mock_rv
    intent = OrderIntent(5, "s1", "2330", IntentType.NEW, Side.BUY, 100, 1, TIF.ROD, None, 0)
    decision = engine.evaluate(intent)
    # Rust error falls through to Python validators which pass for a valid intent
    assert decision.approved is True


@pytest.mark.asyncio
async def test_rejection_feedback_sent_on_unexpected_exception(engine):
    """RiskEngine must emit RiskFeedback to rejection_sink when evaluate raises unexpectedly."""
    from unittest.mock import patch

    rejection_sink = asyncio.Queue()
    engine._rejection_sink = rejection_sink

    intent = OrderIntent(99, "s1", "2330", IntentType.NEW, Side.BUY, 100, 1, TIF.ROD, None, 0)
    engine.intent_queue.put_nowait(intent)

    with patch.object(engine, "evaluate", side_effect=RuntimeError("unexpected boom")):
        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.05)

    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert not rejection_sink.empty(), "RiskFeedback should have been sent to rejection_sink"
    feedback = rejection_sink.get_nowait()
    assert feedback.intent_id == 99
    assert feedback.strategy_id == "s1"
    assert feedback.symbol == "2330"
    assert feedback.reason_code == "risk_engine_error"


@pytest.mark.asyncio
async def test_rejection_sink_overflow_increments_metric(engine):
    """When rejection_sink is full, rejection_sink_overflow_total must be incremented."""
    from unittest.mock import patch

    # Assign a maxsize=1 rejection_sink so the second write overflows
    rejection_sink = asyncio.Queue(maxsize=1)
    engine._rejection_sink = rejection_sink

    # Two intents that will both be rejected (price=0 triggers PRICE_ZERO_OR_NEG)
    intent1 = OrderIntent(10, "s1", "2330", IntentType.NEW, Side.BUY, 0, 1, TIF.ROD, None, 0)
    intent2 = OrderIntent(11, "s1", "2330", IntentType.NEW, Side.BUY, 0, 1, TIF.ROD, None, 0)
    engine.intent_queue.put_nowait(intent1)
    engine.intent_queue.put_nowait(intent2)

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.1)

    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # The sink holds 1 feedback; the second triggered an overflow
    overflow_count = engine.metrics.rejection_sink_overflow_total._value.get()
    assert overflow_count >= 1, (
        f"Expected rejection_sink_overflow_total >= 1, got {overflow_count}"
    )


def test_create_command_propagates_decision_price(engine):
    """create_command must pass decision_price from intent to OrderCommand for TCA."""
    decision_price = 1_234_560_000  # 123456.0 scaled x10000
    base_intent = OrderIntent(6, "s1", "2330", IntentType.NEW, Side.BUY, 100, 1, TIF.ROD, None, 0)
    intent = dataclasses.replace(base_intent, decision_price=decision_price)
    cmd = engine.create_command(intent)
    assert cmd.decision_price == decision_price, (
        f"Expected decision_price={decision_price}, got {cmd.decision_price}; "
        "TCA will be silently zeroed without this passthrough"
    )
