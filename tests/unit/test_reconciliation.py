from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.reconciliation import ReconciliationService


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

    service = ReconciliationService(mock_client, mock_store, {})

    await service.sync_portfolio()
    mock_client.get_positions.assert_called_once()


@pytest.mark.asyncio
async def test_recon_discrepancy_logging():
    # Patch the logger module where ReconciliationService gets it
    with patch("hft_platform.execution.reconciliation.logger") as mock_logger:
        mock_client = MagicMock()
        # Ensure it is awaitable if run in thread? implementation uses asyncio.to_thread
        # but to_thread runs sync function in thread.
        mock_client.get_positions.return_value = [MockPosition("2330", 10, "Action.Buy")]

        service = ReconciliationService(mock_client, MagicMock(), {})

        await service.sync_portfolio()

        # Check that logger.info was called with specific content
        # implementation: logger.info("Portfolio Sync: Broker State", positions=broker_map)
        # We verify one of the info calls contains this message
        calls = [c[0][0] for c in mock_logger.info.call_args_list]
        assert "Portfolio Sync: Broker State" in calls
