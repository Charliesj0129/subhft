import asyncio

import pytest

from hft_platform.main import HFTSystem


@pytest.mark.asyncio
async def test_component_restart_on_failure():
    """
    Chaos Scenario: What happens if OrderAdapter crashes?
    Expected: Supervisor detects crash, triggers HALT, and attempts Restart.
    """
    system = HFTSystem({"shioaji": {"simulation": True}})

    t_sys = asyncio.create_task(system.run())
    await asyncio.sleep(0.5)

    assert system.running

    original_exec = system.order_adapter.execute

    async def crash_exec(*args):
        raise RuntimeError("Chaos Monkey Struck!")

    system.order_adapter.execute = crash_exec

    import time

    from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side, StormGuardState

    cmd = OrderCommand(
        cmd_id=1,
        intent=OrderIntent(1, "test", "2330", IntentType.NEW, Side.BUY, 100, 1, TIF.LIMIT, None, time.time_ns()),
        deadline_ns=time.time_ns() + 10_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
    )
    await system.order_queue.put(cmd)

    await asyncio.sleep(1.5)

    assert not t_sys.done(), "System crashed! Supervisor failed to catch component failure."

    assert system.storm_guard.state == StormGuardState.HALT, "StormGuard did not escalate to HALT."

    t_order = system.tasks.get("order")
    assert t_order is not None
    assert not t_order.done(), "OrderAdapter did not restart successfully."

    system.stop()
    await asyncio.sleep(0.1)
    t_sys.cancel()
    try:
        await t_sys
    except (asyncio.CancelledError, Exception):
        pass
