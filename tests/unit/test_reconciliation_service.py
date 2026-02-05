from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.positions import PositionStore
from hft_platform.execution.reconciliation import ReconciliationService


@pytest.mark.asyncio
async def test_reconciliation_sync_portfolio_logs_remote_positions(tmp_path):
    client = MagicMock()
    pos_sell = SimpleNamespace(code="BBB", quantity=3, direction="Action.Sell")
    client.get_positions.return_value = [{"code": "AAA", "quantity": 2}, pos_sell]

    store = PositionStore()
    service = ReconciliationService(client, store, {"reconciliation": {"heartbeat_threshold_ms": 1}})

    with patch("hft_platform.execution.reconciliation.logger") as mock_logger:
        await service.sync_portfolio()

    # Check that broker state was logged
    mock_logger.info.assert_any_call("Portfolio Sync: Broker State", positions={"AAA": 2, "BBB": -3})
