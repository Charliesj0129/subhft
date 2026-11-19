import asyncio
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.main import HFTSystem


@pytest.mark.asyncio
async def test_risk_rejection(monkeypatch, tmp_path):
    """Verify that RiskEngine blocks excessive orders."""
    sym_file = tmp_path / "symbols.yaml"
    sym_file.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    monkeypatch.setenv("HFT_SYMBOLS", "2330")
    monkeypatch.setenv("SYMBOLS_CONFIG", str(sym_file))
    system = HFTSystem({"risk": {"max_order_size": 2}, "symbols": ["2330"]})

    spy_adapter = MagicMock()
    system.order_adapter = spy_adapter

    t = asyncio.create_task(system.risk_engine.run())

    bad_intent = OrderIntent(
        intent_id=1,
        strategy_id="test",
        symbol="2330",
        side=Side.BUY,
        price=100,
        qty=10,
        tif=TIF.LIMIT,
        intent_type=IntentType.NEW,
    )

    await system.risk_queue.put(bad_intent)
    await asyncio.sleep(0.1)

    spy_adapter.execute.assert_not_called()
    print("Risk Test Passed: Oversized order blocked.")

    t.cancel()


@pytest.mark.asyncio
async def test_storm_guard(monkeypatch, tmp_path):
    """Verify Reconciliation triggers Storm Guard on mismatch."""
    sym_file = tmp_path / "symbols.yaml"
    sym_file.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    monkeypatch.setenv("HFT_SYMBOLS", "2330")
    monkeypatch.setenv("SYMBOLS_CONFIG", str(sym_file))
    system = HFTSystem({"symbols": ["2330"]})
    system.reconciler = system.recon_service

    system.order_adapter.cancel_all = MagicMock()

    from hft_platform.execution.positions import Position

    system.position_store.positions["test:test:2330"] = Position("test", "test", "2330", net_qty=0)

    system.reconciler.client = MagicMock()
    pos_remote = MagicMock()
    pos_remote.code = "2330"
    pos_remote.quantity = 50
    pos_remote.direction = "Action.Buy"
    system.reconciler.client.get_positions.return_value = [pos_remote]

    await system.reconciler.sync_portfolio()

    discrepancies = system.reconciler._last_discrepancies
    assert len(discrepancies) > 0, "Expected reconciliation to detect position mismatch"
    assert discrepancies[0].symbol == "2330"
    assert discrepancies[0].local_qty == 0
    assert discrepancies[0].broker_qty == 50


if __name__ == "__main__":
    asyncio.run(test_risk_rejection())
    asyncio.run(test_storm_guard())
