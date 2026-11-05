"""``OrderGateway.list_open_orders``: refresh from the broker, list working orders, never fake an empty answer."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.shioaji.order_gateway import OrderGateway


def _trade(status: str) -> Any:
    return SimpleNamespace(status=SimpleNamespace(status=SimpleNamespace(value=status)))


def _gateway(api: Any) -> OrderGateway:
    client = MagicMock()
    client.api = api
    return OrderGateway(client)


def test_list_open_orders_refreshes_then_returns_working_orders_only():
    trades = [_trade(s) for s in ("Submitted", "PartFilled", "Filled", "Cancelled", "Failed", "PendingSubmit")]
    api = SimpleNamespace(
        futopt_account="FUT", stock_account=None, update_status=MagicMock(), list_trades=MagicMock(return_value=trades)
    )

    out = _gateway(api).list_open_orders(timeout_ms=1234)

    api.update_status.assert_called_once_with("FUT", timeout=1234)
    assert [t.status.status.value for t in out] == ["Submitted", "PartFilled", "PendingSubmit"]


def test_list_open_orders_raises_when_broker_refresh_fails():
    api = SimpleNamespace(
        futopt_account="FUT",
        stock_account=None,
        update_status=MagicMock(side_effect=TimeoutError("30s")),
        list_trades=MagicMock(return_value=[]),
    )

    with pytest.raises(TimeoutError):
        _gateway(api).list_open_orders()


def test_list_open_orders_raises_without_account():
    api = SimpleNamespace(futopt_account=None, stock_account=None, update_status=MagicMock(), list_trades=MagicMock())

    with pytest.raises(RuntimeError):
        _gateway(api).list_open_orders()
