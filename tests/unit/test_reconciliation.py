from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.risk.storm_guard import StormGuard


class MockPosition:
    def __init__(self, code, quantity, direction):
        self.code = code
        self.quantity = quantity
        self.direction = direction


@pytest.mark.asyncio
async def test_recon_sync_portfolio():
    mock_client = MagicMock()
    mock_client.get_positions.return_value = [MockPosition("2330", 5, "Action.Buy")]
    mock_store = MagicMock()
    mock_store.positions = {}

    service = ReconciliationService(mock_client, mock_store, {}, storm_guard=StormGuard())

    await service.sync_portfolio()
    mock_client.get_positions.assert_called_once()


@pytest.mark.asyncio
async def test_recon_discrepancy_logging():
    with patch("hft_platform.execution.reconciliation.logger") as mock_logger:
        mock_client = MagicMock()
        mock_client.get_positions.return_value = [MockPosition("2330", 10, "Action.Buy")]

        service = ReconciliationService(mock_client, MagicMock(), {}, storm_guard=StormGuard())

        await service.sync_portfolio()

        calls = [c[0][0] for c in mock_logger.debug.call_args_list]
        assert "Portfolio Sync: Broker State" in calls
