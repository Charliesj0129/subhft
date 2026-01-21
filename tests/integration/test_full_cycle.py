import asyncio
import time
from unittest.mock import MagicMock

import pytest

from hft_platform.execution.normalizer import RawExecEvent
from hft_platform.services.system import HFTSystem
from hft_platform.strategy.base import BaseStrategy


class MockStrategy(BaseStrategy):
    def on_stats(self, event):
        pass

    def on_tick(self, event):
        pass


@pytest.mark.asyncio
async def test_full_order_lifecycle():
    """
    Verifies: Strategy -> Bus -> Risk -> OrderAdapter -> Gateway (Mock) -> Fill -> Bus -> Strategy
    """
    # 1. Setup Data
    settings = {
        "kafka": {"enabled": False},
        "wal": {"enabled": False},
        "risk": {"max_order_size": 100},
        "shioaji": {"simulation": True},
        "paths": {
            # Mock paths or relying on defaults which might fail if files missing?
            # HFTSystem loads configs. We assume test env has them or we mock.
            "symbols": "config/symbols.yaml",
            "strategy_limits": "config/base/strategy_limits.yaml",
            "order_adapter": "config/base/order_adapter.yaml",
        },
    }

    # We need ensure config files exist or mock them.
    # For integration test, usually we rely on "config/base" existing.

    system = HFTSystem(settings)

    # Register a Test Strategy
    strat = MockStrategy("USER1")

    # Patch handle_event to bypass dispatch logic for custom trigger
    # Or just use on_tick if we send a TickEvent?
    # Let's use a custom mechanism: override handle_event

    original_handle = strat.handle_event

    def handle_override(ctx, event):
        if getattr(event, "topic", "") == "trigger":
            strat.ctx = ctx
            strat.buy("2330", 100.0, 1)
            return strat._generated_intents
        return original_handle(ctx, event)

    strat.handle_event = handle_override
    system.strategy_runner.strategies = [strat]

    # Mock Shioaji API via OrderAdapter's client
    mock_api = MagicMock()
    mock_api.place_order.return_value = {"seq_no": "test-seq-1", "order_id": "oid-1"}
    mock_api.stock_account = None
    mock_api.futopt_account = None

    # Mock Contract
    mock_contract = MagicMock()
    mock_contract.code = "2330"
    mock_api.Contracts.Stocks.TSE.__getitem__.return_value = mock_contract

    # Inject Mock API into ShioajiClient
    system.client.api = mock_api
    system.client.logged_in = True  # Fake login

    # 2. Components to Run
    # HFTSystem.run() runs everything, but blocks. We run components individually for control?
    # Or start system.run() as task.
    # Start individually for easier cleanup/control validation.

    system.running = True
    system.exec_service.running = True
    system.risk_engine.running = True
    system.order_adapter.running = True
    system.strategy_runner.running = True

    tasks = [
        asyncio.create_task(system.exec_service.run()),
        asyncio.create_task(system.risk_engine.run()),
        asyncio.create_task(system.order_adapter.run()),
        asyncio.create_task(system.strategy_runner.run()),
    ]

    await asyncio.sleep(0.1)

    # 3. Trigger Strategy
    # Publish a "Trigger" event.
    # Use a dummy object that has .topic
    class TriggerEvent:
        symbol = "2330"
        topic = "trigger"
        strategy_id = None

    await system.bus.publish(TriggerEvent())

    # 4. Verify Order Placed
    # Poll
    start_wait = time.time()
    while time.time() - start_wait < 2.0:
        if mock_api.place_order.call_count > 0:
            break
        await asyncio.sleep(0.05)

    mock_api.place_order.assert_called_once()
    args, kwargs = mock_api.place_order.call_args
    # args[0] is contract, args[1] is order
    order_obj = args[1]

    assert order_obj.price == 100.0
    assert order_obj.quantity == 1

    print("Action Verified: Strategy -> Risk -> API")

    # 5. Simulate Fill (Return Trip)
    # ExecService reads from raw_exec_queue
    ts = time.time_ns()
    raw_deal_data = {
        "code": "2330",
        "price": 100.0,
        "quantity": 1,
        "seq_no": "test-seq-1",
        "ord_no": "oid-1",
        "action": "Buy",
        "custom_field": "USER1",
        "ts": ts,
    }

    # Execution Service expects RawExecEvent
    raw_event = RawExecEvent(topic="deal", data=raw_deal_data, ingest_ts_ns=ts)

    await system.raw_exec_queue.put(raw_event)

    # Wait for processing
    await asyncio.sleep(0.5)

    # 6. Verify Position Update
    # Key format in PositionStore
    key = "sim-account-01:USER1:2330"
    # Note: ShioajiClient default account might need checking if "sim-account-01" logic is in normalizer?
    # Checked normalizer.py: account_id=str(get("account_id") or "sim-account-01") -> Correct.

    pos_obj = system.position_store.positions.get(key)
    # Check
    assert pos_obj is not None, f"Position keys: {system.position_store.positions.keys()}"
    assert pos_obj.net_qty == 1

    print(f"Position Verified: {pos_obj.net_qty}")

    # Cleanup
    system.stop()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(test_full_order_lifecycle())
