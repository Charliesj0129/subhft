from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.execution.positions import PositionStore
from hft_platform.execution.positions import Position
from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.risk.storm_guard import StormGuard


class _StockPosition:
    def __init__(self, code: str, quantity: int, direction: str = "Long") -> None:
        self.code = code
        self.quantity = quantity
        self.direction = direction


class _FuturePosition:
    def __init__(self, code: str, quantity: int, direction: str = "Long") -> None:
        self.code = code
        self.quantity = quantity
        self.direction = direction


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

    # Broker state is logged at DEBUG (downgraded from INFO to reduce 5 s-cadence spam)
    mock_logger.debug.assert_any_call("Portfolio Sync: Broker State", positions={"AAA": 2, "BBB": -3})

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
async def test_reconciliation_dedupes_duplicate_code_across_broker_position_types():
    """Duplicate Shioaji code across stock/futopt snapshots must not be summed.

    AccountGateway concatenates stock_account and futopt_account positions. When
    both account facets return the same literal code, reconciliation runs at
    symbol scope and cannot safely add them together without inflating
    broker_qty.
    """
    client = MagicMock()
    client.get_positions.return_value = [
        _StockPosition("2330", 5, "Long"),
        _FuturePosition("2330", 5, "Long"),
    ]

    store = PositionStore()
    store.positions["acct:R47:2330"] = Position(
        account_id="acct",
        strategy_id="R47",
        symbol="2330",
        net_qty=5,
        avg_price_scaled=1_000_000,
    )

    service = ReconciliationService(
        client,
        store,
        {"reconciliation": {"heartbeat_threshold_ms": 1}, "symbols": [{"code": "2330"}]},
        storm_guard=StormGuard(),
    )

    await service.sync_portfolio()

    assert service._last_discrepancies == []


@pytest.mark.asyncio
async def test_reconciliation_ignores_legacy_recovery_when_live_position_already_exists():
    """Legacy account:symbol recovery must not double-count over a live position."""
    client = MagicMock()
    client.get_positions.return_value = [_StockPosition("2330", 5, "Long")]

    store = PositionStore()
    store.positions["acct:R47:2330"] = Position(
        account_id="acct",
        strategy_id="R47",
        symbol="2330",
        net_qty=5,
        avg_price_scaled=1_000_000,
    )
    store._recovery_positions["acct:2330"] = {
        "account_id": "acct",
        "strategy_id": "",
        "symbol": "2330",
        "net_qty": 5,
        "avg_price_scaled": -1,
        "realized_pnl_scaled": 0,
        "fees_scaled": 0,
    }

    service = ReconciliationService(
        client,
        store,
        {"reconciliation": {"heartbeat_threshold_ms": 1}, "symbols": [{"code": "2330"}]},
        storm_guard=StormGuard(),
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

    # Override critical debounce to 1 to test broker-zero debounce in isolation
    service._critical_drift_debounce = 1

    with patch.object(service, "_trigger_halt", new_callable=AsyncMock) as halt_mock:
        await service.sync_portfolio()
        halt_mock.assert_not_called()
        assert service._last_discrepancies == []

        await service.sync_portfolio()
        halt_mock.assert_called_once()


@pytest.mark.asyncio
async def test_critical_discrepancy_debounce_prevents_immediate_halt():
    """Critical futures discrepancy requires multiple consecutive observations before HALT."""
    client = MagicMock()
    client.get_positions.return_value = []

    store = PositionStore()
    store.load_recovery(
        account_id="acct",
        symbol="TXFD6",
        net_qty=-1,
        avg_price_scaled=-1,
        realized_pnl_scaled=0,
        fees_scaled=0,
        strategy_id="",
    )

    service = ReconciliationService(
        client,
        store,
        {"reconciliation": {"broker_zero_debounce_observations": 1}},
        storm_guard=StormGuard(),
    )
    service._critical_drift_debounce = 3

    with patch.object(service, "_trigger_halt", new_callable=AsyncMock) as halt_mock:
        # Observation 1: debounce — no HALT
        await service.sync_portfolio()
        halt_mock.assert_not_called()
        assert service._critical_drift_streak == 1

        # Observation 2: still debouncing
        await service.sync_portfolio()
        halt_mock.assert_not_called()
        assert service._critical_drift_streak == 2

        # Observation 3: debounce met — HALT triggered
        await service.sync_portfolio()
        halt_mock.assert_called_once()


@pytest.mark.asyncio
async def test_halt_not_retriggered_while_already_halted():
    """Once HALT is triggered, recon must not re-trigger to avoid resetting de-escalation."""
    client = MagicMock()
    client.get_positions.return_value = []

    store = PositionStore()
    store.load_recovery(
        account_id="acct",
        symbol="TXFD6",
        net_qty=-1,
        avg_price_scaled=-1,
        realized_pnl_scaled=0,
        fees_scaled=0,
        strategy_id="",
    )

    service = ReconciliationService(
        client,
        store,
        {"reconciliation": {"broker_zero_debounce_observations": 1}},
        storm_guard=StormGuard(),
    )
    service._critical_drift_debounce = 1

    with patch.object(service, "_trigger_halt", new_callable=AsyncMock) as halt_mock:
        # First sync: triggers HALT
        await service.sync_portfolio()
        assert halt_mock.call_count == 1

        # Second sync: discrepancy persists but _halt_triggered=True — no re-trigger
        await service.sync_portfolio()
        assert halt_mock.call_count == 1  # still 1, NOT 2
        assert service._critical_drift_streak == 2
