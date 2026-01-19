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

    # Start System
    t_sys = asyncio.create_task(system.run())
    await asyncio.sleep(0.5)  # Warmup

    assert system.running

    # Inject Poison Pill into OrderAdapter
    # We monkeypatch the execute method on the live instance
    # System holds ref to order_adapter

    original_exec = system.order_adapter.execute

    async def crash_exec(*args):
        raise RuntimeError("Chaos Monkey Struck!")

    system.order_adapter.execute = crash_exec

    # Trigger Execution
    import time

    from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side, StormGuardState

    cmd = OrderCommand(
        cmd_id=1,
        intent=OrderIntent(1, "test", "2330", IntentType.NEW, Side.BUY, 100, 1, TIF.LIMIT, None, time.time_ns()),
        deadline_ns=time.time_ns() + 10_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
    )
    await system.order_queue.put(cmd)

    # Wait for Supervisor (ticks every 1s)
    await asyncio.sleep(1.5)

    # Assertions
    # 1. System Main Task should be ALIVE (Supervisor caught exception)
    assert not t_sys.done(), "System crashed! Supervisor failed to catch component failure."

    # 2. StormGuard should be HALT
    assert system.storm_guard.state == StormGuardState.HALT, "StormGuard did not escalate to HALT."

    # 3. OrderAdapter should be running (Restarted) or at least tasks dict has new task
    # We can check if 'order' task is done (the old one) or replaced.
    # Implementation details: supervisor replaces self.tasks['order']

    t_order = system.tasks.get("order")
    assert t_order is not None
    assert not t_order.done(), "OrderAdapter did not restart successfully."

    # Cleanup
    system.stop()
    await asyncio.sleep(0.1)
    t_sys.cancel()
    try:
        await t_sys
    except:
        pass
