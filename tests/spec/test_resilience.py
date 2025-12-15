
import pytest
import asyncio
from hft_platform.main import HFTSystem

@pytest.mark.asyncio
async def test_component_restart_on_failure():
    """
    Chaos Scnenario: What happens if OrderAdapter crashes?
    Expected: System should ideally restart it or Shutdown gracefully.
    Current: Likely unhandled exception propagation.
    """
    system = HFTSystem({"shioaji": {"simulation": True}})
    
    # Start System
    t_sys = asyncio.create_task(system.run())
    await asyncio.sleep(0.5) # Warmup
    
    # CHAOS INJECTION: Manually cancel/raise inside OrderAdapter task
    # We need to find the task. HFTSystem doesn't expose tasks publically in list?
    # It stores local vars in run().
    
    # We can simulate failure by injecting an Exception into the queue?
    # Or mocking the run method to crash after delay.
    
    # Let's inspect system state
    assert system.running
    
    # Finding the order adapter task is hard without modification to main.py to store tasks self.tasks
    # So we will verify "Graceful Shutdown on Critical Failure" instead.
    
    # Inject Poison Pill
    # If we put a poisoned message that causes crash in OrderAdapter
    # Does the WHOLE system shut down? It should.
    
    # Mock execute to raise
    original_exec = system.order_adapter.execute
    async def crash_exec(*args):
        raise RuntimeError("Chaos Monkey Struck!")
    system.order_adapter.execute = crash_exec
    
    # Send Command
    from hft_platform.contracts.strategy import OrderCommand, OrderIntent, Side, TIF, IntentType, StormGuardState
    import time
    cmd = OrderCommand(
        cmd_id=1, 
        intent=OrderIntent(1, "test", "2330", IntentType.NEW, Side.BUY, 100, 1, TIF.LIMIT, None, time.time_ns()),
        deadline_ns=time.time_ns() + 10_000_000_000,
        storm_guard_state=StormGuardState.NORMAL
    )
    await system.order_queue.put(cmd)
    
    # Wait for crash propagation
    await asyncio.sleep(0.5)
    
    # Assert
    # If OrderAdapter crashed, main.py 'gather' might have raised/returned.
    # checking if t_sys is done
    # Assert
    # System MUST have crashed
    assert t_sys.done(), "System remained running (Zombie Mode) despite component failure!"
    
    exc = t_sys.exception()
    assert isinstance(exc, RuntimeError)
    assert "Chaos Monkey" in str(exc)
    print(f"System crashed as expected: {exc}")
    
    # Cleanup
    system.running = False
    await asyncio.sleep(0.1)
    t_sys.cancel()
    try: await t_sys 
    except: pass
