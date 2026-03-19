from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.risk.storm_guard import StormGuard


class MockPosition:
    def __init__(self, symbol: str, net_qty: int) -> None:
        self.symbol = symbol
        self.net_qty = net_qty


@pytest.mark.asyncio
async def test_reconciliation_metric_updated_with_discrepancy_count() -> None:
    """When broker and local positions differ, reconciliation_discrepancy_count is set to the discrepancy count."""
    mock_client = MagicMock()
    # Broker has 10 shares of 2330; local store is empty → 1 discrepancy
    mock_client.get_positions.return_value = [
        SimpleNamespace(code="2330", quantity=10, direction="Action.Buy")
    ]

    mock_store = MagicMock()
    mock_store.positions = {}

    mock_metrics = MagicMock()

    with patch(
        "hft_platform.execution.reconciliation.MetricsRegistry.get",
        return_value=mock_metrics,
    ):
        service = ReconciliationService(mock_client, mock_store, {}, storm_guard=StormGuard())
        await service.sync_portfolio()

    mock_metrics.reconciliation_discrepancy_count.set.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_reconciliation_metric_zero_when_no_discrepancies() -> None:
    """When broker and local positions match exactly, reconciliation_discrepancy_count is set to 0."""
    mock_client = MagicMock()
    mock_client.get_positions.return_value = [
        SimpleNamespace(code="2330", quantity=5, direction="Action.Buy")
    ]

    local_pos = MockPosition(symbol="2330", net_qty=5)
    mock_store = MagicMock()
    mock_store.positions = {"acct:strat:2330": local_pos}

    mock_metrics = MagicMock()

    with patch(
        "hft_platform.execution.reconciliation.MetricsRegistry.get",
        return_value=mock_metrics,
    ):
        service = ReconciliationService(mock_client, mock_store, {}, storm_guard=StormGuard())
        await service.sync_portfolio()

    mock_metrics.reconciliation_discrepancy_count.set.assert_called_once_with(0)


@pytest.mark.asyncio
async def test_reconciliation_metric_reflects_multiple_discrepancies() -> None:
    """When multiple symbols have differing positions, the metric count equals the number of discrepant symbols."""
    mock_client = MagicMock()
    mock_client.get_positions.return_value = [
        SimpleNamespace(code="2330", quantity=10, direction="Action.Buy"),
        SimpleNamespace(code="2317", quantity=20, direction="Action.Buy"),
    ]

    mock_store = MagicMock()
    mock_store.positions = {}  # local is empty → 2 discrepancies

    mock_metrics = MagicMock()

    with patch(
        "hft_platform.execution.reconciliation.MetricsRegistry.get",
        return_value=mock_metrics,
    ):
        service = ReconciliationService(mock_client, mock_store, {}, storm_guard=StormGuard())
        await service.sync_portfolio()

    mock_metrics.reconciliation_discrepancy_count.set.assert_called_once_with(2)


@pytest.mark.asyncio
async def test_reconciliation_metric_not_updated_on_sync_failure() -> None:
    """When the broker client raises an exception, the metric is not updated."""
    mock_client = MagicMock()
    mock_client.get_positions.side_effect = RuntimeError("broker unavailable")

    mock_store = MagicMock()
    mock_store.positions = {}

    mock_metrics = MagicMock()

    with patch(
        "hft_platform.execution.reconciliation.MetricsRegistry.get",
        return_value=mock_metrics,
    ):
        service = ReconciliationService(mock_client, mock_store, {}, storm_guard=StormGuard())
        # sync_portfolio now raises on failure (WU-04 resilience change)
        with pytest.raises(RuntimeError, match="broker unavailable"):
            await service.sync_portfolio()

    mock_metrics.reconciliation_discrepancy_count.set.assert_not_called()
