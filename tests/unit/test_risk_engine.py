import asyncio

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
