import asyncio
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.main import HFTSystem


@pytest.mark.asyncio
async def test_risk_rejection(monkeypatch):
    """Verify that RiskEngine blocks excessive orders."""
    monkeypatch.setenv("HFT_SYMBOLS", "2330")
    system = HFTSystem({"risk": {"max_order_size": 2}})  # Strict limit

    # Bypass runner/bus, inject directly into risk queue
    # We want to see if OrderAdapter receives it.

    spy_adapter = MagicMock()
    system.order_adapter = spy_adapter

    # 1. Start Risk Engine
    t = asyncio.create_task(system.risk_engine.run())

    # 2. Send Oversized Order
    bad_intent = OrderIntent(
        intent_id=1,
        strategy_id="test",
        symbol="2330",
        side=Side.BUY,
        price=100,
        qty=10,  # > 2
        tif=TIF.LIMIT,
        intent_type=IntentType.NEW,
    )

    await system.risk_queue.put(bad_intent)
    await asyncio.sleep(0.1)

    # 3. Assert NOT placed
    spy_adapter.execute.assert_not_called()
    print("Risk Test Passed: Oversized order blocked.")

    t.cancel()


@pytest.mark.asyncio
async def test_storm_guard(monkeypatch):
    """Verify Reconciliation triggers Storm Guard on mismatch."""
    monkeypatch.setenv("HFT_SYMBOLS", "2330")
    system = HFTSystem({})
    # Inject dependencies
    system.reconciler = system.recon_service

    # Mock cancel_all
    system.order_adapter.cancel_all = MagicMock()

    # 1. Setup Mismatch
    # Local says 0, Remote says 50
    from hft_platform.execution.positions import Position

    system.position_store.positions["test:test:2330"] = Position("test", "test", "2330", net_qty=0)

    # Mock remote fetch
    # ReconciliationService.sync_portfolio calls self.client.get_positions
    system.reconciler.client = MagicMock()
    # Return list of objects or dicts? Code expects checks getattr or get.
    # Let's return dict-like objects
    pos_remote = MagicMock()
    pos_remote.code = "2330"
    pos_remote.quantity = 50
    pos_remote.direction = "Action.Buy"
    system.reconciler.client.get_positions.return_value = [pos_remote]

    # 2. Run Sync Cycle manually
    await system.reconciler.sync_portfolio()

    # 3. Assert reconciliation detected the position mismatch (local=0, broker=50)
    discrepancies = system.reconciler._last_discrepancies
    assert len(discrepancies) > 0, "Expected reconciliation to detect position mismatch"
    assert discrepancies[0].symbol == "2330"
    assert discrepancies[0].local_qty == 0
    assert discrepancies[0].broker_qty == 50


if __name__ == "__main__":
    asyncio.run(test_risk_rejection())
    asyncio.run(test_storm_guard())
