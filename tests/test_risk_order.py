import asyncio
import time

import pytest
import yaml

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, StormGuardState
from hft_platform.order.adapter import OrderAdapter, OrderCommand
from hft_platform.risk.engine import RiskEngine
from hft_platform.risk.validators import PriceBandValidator, StormGuardFSM

# Mock Config
MOCK_CONFIG = {
    "global_defaults": {"price_band_ticks": 10},
    "strategies": {"TEST_STRAT": {"max_notional": 1000}},
    "storm_guard": {"halt_threshold": -100},
}
MOCK_ADAPTER_CONFIG = {
    "rate_limits": {"shioaji_soft_cap": 5, "shioaji_hard_cap": 10},
}


class MockClient:
    pass


@pytest.mark.asyncio
async def test_risk_validators():
    # 1. Price Band
    validator = PriceBandValidator(MOCK_CONFIG)
    intent = OrderIntent(1, "TEST_STRAT", "2330", IntentType.NEW, Side.BUY, -100, 1, timestamp_ns=0)
    ok, reason = validator.check(intent)
    assert not ok
    assert "PRICE_ZERO" in reason


@pytest.mark.asyncio
async def test_storm_guard_transition():
    fsm = StormGuardFSM(MOCK_CONFIG)
    assert fsm.state == StormGuardState.NORMAL

    # Simulate Drawdown
    fsm.update_pnl(-50)
    assert fsm.state == StormGuardState.NORMAL

    fsm.update_pnl(-150)  # Below -100 halt
    assert fsm.state == StormGuardState.HALT

    intent = OrderIntent(2, "TEST_STRAT", "2330", IntentType.NEW, Side.BUY, 1000, 1, timestamp_ns=0)
    ok, reason = fsm.validate(intent)
    assert not ok
    assert "HALT" in reason


@pytest.mark.asyncio
async def test_risk_engine_pipeline():
    i_q = asyncio.Queue()
    o_q = asyncio.Queue()
    # Write temp config
    import yaml

    with open("config/test_limits.yaml", "w") as f:
        yaml.dump(MOCK_CONFIG, f)

    engine = RiskEngine("config/test_limits.yaml", i_q, o_q)

    # Reject Case
    bad_intent = OrderIntent(3, "TEST_STRAT", "2330", IntentType.NEW, Side.BUY, -1, 1, timestamp_ns=0)
    await i_q.put(bad_intent)

    # Run engine briefly
    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.1)

    assert o_q.empty()  # Should be dropped/rejected

    # Approve Case
    good_intent = OrderIntent(4, "TEST_STRAT", "2330", IntentType.NEW, Side.BUY, 5000000, 1, timestamp_ns=0)
    # 5000000/10000 * 1 = 500 notional < 1000 limit
    await i_q.put(good_intent)
    await asyncio.sleep(0.1)

    cmd = await o_q.get()
    assert cmd.intent.intent_id == 4

    engine.running = False
    task.cancel()


@pytest.mark.asyncio
async def test_circuit_breaker():
    # Setup
    q = asyncio.Queue()
    with open("config/test_adapter.yaml", "w") as f:
        yaml.dump(MOCK_ADAPTER_CONFIG, f)

    adapter = OrderAdapter("config/test_adapter.yaml", q, MockClient())
    adapter.circuit_breaker.threshold = 2
    adapter.circuit_breaker.timeout_s = 1

    # Run
    task = asyncio.create_task(adapter.run())

    # Inject failures
    deadline = time.time_ns() + 10_000_000_000  # +10s
    cmd = OrderCommand(1, OrderIntent(1, "S", "C", 0, 0, 0, 0, target_order_id="1"), deadline, 0)

    # We need to mock execute to raise exception, but execute checks limits first.
    # We can patch client to raise
    class BalClient:
        pass

    adapter.client = BalClient()  # No api object -> AttributeError -> caught as "Broker Error"

    await q.put(cmd)
    await q.put(cmd)  # 2 failures
    await asyncio.sleep(0.1)

    assert adapter.circuit_breaker.failure_count >= 2
    assert adapter.circuit_breaker.open_until > 0

    # 3rd should be rejected by CB immediately
    await q.put(cmd)
    await asyncio.sleep(0.1)

    adapter.running = False
    task.cancel()
