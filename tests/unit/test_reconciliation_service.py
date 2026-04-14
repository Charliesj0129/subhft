from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.execution.positions import PositionStore
from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.risk.storm_guard import StormGuard


@pytest.mark.asyncio
async def test_reconciliation_sync_portfolio_logs_remote_positions(tmp_path):
    client = MagicMock()
    pos_sell = SimpleNamespace(code="BBB", quantity=3, direction="Action.Sell")
    client.get_positions.return_value = [{"code": "AAA", "quantity": 2}, pos_sell]

    store = PositionStore()
    service = ReconciliationService(
        client, store, {"reconciliation": {"heartbeat_threshold_ms": 1}}, storm_guard=StormGuard()
    )

    with patch("hft_platform.execution.reconciliation.logger") as mock_logger:
        await service.sync_portfolio()

    # Check that broker state was logged
    mock_logger.info.assert_any_call("Portfolio Sync: Broker State", positions={"AAA": 2, "BBB": -3})

    # Verify discrepancies were computed (local store is empty, so both symbols differ)
    discreps = service._last_discrepancies
    assert isinstance(discreps, list)
    assert len(discreps) == 2, f"Expected 2 discrepancies, got {len(discreps)}"

    by_symbol = {d.symbol: d for d in discreps}
    assert "AAA" in by_symbol
    assert "BBB" in by_symbol
    # local=0, broker=2 → diff = 0 - 2 = -2
    assert by_symbol["AAA"].diff == -2
    assert by_symbol["AAA"].local_qty == 0
    assert by_symbol["AAA"].broker_qty == 2
    # local=0, broker=-3 → diff = 0 - (-3) = 3
    assert by_symbol["BBB"].diff == 3
    assert by_symbol["BBB"].local_qty == 0
    assert by_symbol["BBB"].broker_qty == -3


@pytest.mark.asyncio
async def test_reconciliation_includes_pending_recovery_positions():
    client = MagicMock()
    client.get_positions.return_value = [SimpleNamespace(code="TMFD6", quantity=1, direction="Long")]

    store = PositionStore()
    store.load_recovery(
        account_id="acct",
        symbol="TMFD6",
        net_qty=1,
        avg_price_scaled=-1,
        realized_pnl_scaled=0,
        fees_scaled=0,
        strategy_id="",
    )

    service = ReconciliationService(
        client, store, {"reconciliation": {"heartbeat_threshold_ms": 1}}, storm_guard=StormGuard()
    )

    await service.sync_portfolio()

    assert service._last_discrepancies == []


@pytest.mark.asyncio
async def test_broker_empty_snapshot_is_debounced_before_halt():
    client = MagicMock()
    client.get_positions.return_value = []

    store = PositionStore()
    store.load_recovery(
        account_id="acct",
        symbol="TMFD6",
        net_qty=-1,
        avg_price_scaled=-1,
        realized_pnl_scaled=0,
        fees_scaled=0,
        strategy_id="",
    )

    service = ReconciliationService(
        client,
        store,
        {"reconciliation": {"heartbeat_threshold_ms": 1, "broker_zero_debounce_observations": 2}},
        storm_guard=StormGuard(),
    )

    with patch.object(service, "_trigger_halt", new_callable=AsyncMock) as halt_mock:
        await service.sync_portfolio()
        halt_mock.assert_not_called()
        assert service._last_discrepancies == []

        await service.sync_portfolio()
        halt_mock.assert_called_once()
