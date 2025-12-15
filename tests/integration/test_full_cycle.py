
import pytest
import asyncio
import time
from unittest.mock import MagicMock
from hft_platform.main import HFTSystem
from hft_platform.contracts.strategy import Side, TIF, IntentType, OrderIntent
from hft_platform.strategy.base import BaseStrategy
from hft_platform.execution.normalizer import RawExecEvent

class MockStrategy(BaseStrategy):
    def on_tick(self, symbol, mid, spread):
        pass

@pytest.mark.asyncio
async def test_full_order_lifecycle():
    """
    Verifies: Strategy -> Bus -> Risk -> OrderAdapter -> Gateway (Mock) -> Fill -> Bus -> Strategy
    """
    # 1. Setup Wrapper used in main.py
    # We use a trimmed down setup to avoid full heavy modules if possible, 
    # but for E2E we want the real HFTSystem logic.
    
    # We mock settings
    settings = {
        "kafka": {"enabled": False},
        "wal": {"enabled": False},
        "risk": {"max_order_size": 100},
        "shioaji": {"simulation": True} # Triggers mock connection
    }
    
    system = HFTSystem(settings)
    
    # Register a Test Strategy
    strat = MockStrategy("USER1")
    
    # Let's patch on_event to place order upon receiving specific trigger
    
    def on_event_override(event):
        if isinstance(event, dict) and event.get("type") == "trigger":
            strat.buy("2330", 100.0, 1)
            
    strat.on_event = on_event_override
    system.strategy_runner.register(strat)
    
    # Mock Shioaji API in OrderAdapter
    mock_api = MagicMock()
    mock_api.place_order.return_value = {"seq_no": "test-seq-1", "order_id": "oid-1"}
    
    # Configure contract
    mock_contract = MagicMock()
    mock_contract.code = "2330"
    mock_api.Contracts.Stocks.TSE.__getitem__.return_value = mock_contract
    
    # Inject Mock API
    system.order_adapter.client.api = mock_api
    
    # Set running flag for HFTSystem components
    system.running = True

    # Start System Logic 
    workers = [
        asyncio.create_task(system.process_execution_events()),
        asyncio.create_task(system.strategy_runner.run()),
        asyncio.create_task(system.risk_engine.run()),
        asyncio.create_task(system.order_adapter.run())
    ]
    
    await asyncio.sleep(0.1) # Startup
    
    # 2. Trigger Strategy
    # Normalize -> Bus
    trigger_event = {"symbol": "2330", "type": "trigger", "mid_price": 100.0}
    await system.bus.publish(trigger_event)
    
    await asyncio.sleep(0.1)
    
    # 3. Verify Order Placed
    # Poll for call
    for _ in range(10):
        if mock_api.place_order.call_count > 0:
            break
        await asyncio.sleep(0.1)
        
    mock_api.place_order.assert_called_once()
    args, kwargs = mock_api.place_order.call_args
    contract = args[0]
    order_obj = args[1]
    
    assert contract.code == "2330"
    assert order_obj.price == 100.0
    assert order_obj.quantity == 1
    
    print("Action Verified: Strategy -> Risk -> API")
    
    # 4. Simulate Fill (The Return Trip)
    raw_deal_data = {
        "topic": "deal",
        "code": "2330",
        "price": 100.0,
        "quantity": 1,
        "seq_no": "test-seq-1",
        "ord_no": "oid-1",
        "action": "Buy",
        "custom_field": "USER1",
        "ts": int(time.time_ns())
    }
    
    # Wrap in RawExecEvent
    raw_event = RawExecEvent(topic="deal", data=raw_deal_data, ingest_ts_ns=time.time_ns())
    
    await system.raw_exec_queue.put(raw_event)
    
    await asyncio.sleep(1.0)
    
    # 5. Verify Position Update
    # PositionStore key: acc:strat:sym
    key = "sim-account-01:USER1:2330"

    pos_obj = system.position_store.positions.get(key)
    pos = pos_obj.net_qty if pos_obj else 0
    
    print(f"Position Verified: {pos}")
    assert pos == 1
    
    # Cleanup
    for w in workers: w.cancel()

if __name__ == "__main__":
    from hft_platform.utils.logging import configure_logging
    configure_logging()
    asyncio.run(test_full_order_lifecycle())
