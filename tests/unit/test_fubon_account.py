"""Tests for Fubon account gateway — AccountProvider protocol conformance."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.fubon.account_gateway import (
    FubonAccountGateway,
    _unwrap_list,
    _unwrap_scalar,
)


# ---------------------------------------------------------------------------
# AccountProvider protocol (mirrors the canonical definition)
# ---------------------------------------------------------------------------
@runtime_checkable
class AccountProvider(Protocol):
    def get_positions(self) -> list[Any]: ...
    def get_account_balance(self, account: Any = None) -> Any: ...
    def get_margin(self, account: Any = None) -> Any: ...
    def list_position_detail(self, account: Any = None) -> list[Any]: ...
    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> list[Any]: ...


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def mock_sdk() -> MagicMock:
    """Return a mock Fubon Neo SDK with ``accounting`` sub-object."""
    return MagicMock()


@pytest.fixture()
def mock_account() -> MagicMock:
    return MagicMock(spec_set=[], name="test_account")


@pytest.fixture()
def gateway(mock_sdk: MagicMock, mock_account: MagicMock) -> FubonAccountGateway:
    return FubonAccountGateway(sdk=mock_sdk, account=mock_account)


# ---------------------------------------------------------------------------
# Unwrap helpers
# ---------------------------------------------------------------------------
class TestUnwrapHelpers:
    def test_unwrap_list_with_data(self) -> None:
        obj = MagicMock(data=[1, 2, 3])
        assert _unwrap_list(obj) == [1, 2, 3]

    def test_unwrap_list_none(self) -> None:
        assert _unwrap_list(None) == []

    def test_unwrap_list_no_data_attr(self) -> None:
        assert _unwrap_list("plain_string") == []

    def test_unwrap_scalar_with_data(self) -> None:
        obj = MagicMock(data={"balance": 100})
        assert _unwrap_scalar(obj) == {"balance": 100}

    def test_unwrap_scalar_none(self) -> None:
        assert _unwrap_scalar(None) is None


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------
class TestProtocolConformance:
    def test_isinstance_account_provider(self, gateway: FubonAccountGateway) -> None:
        assert isinstance(gateway, AccountProvider)


# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------
class TestGetPositions:
    def test_returns_list_from_inventories(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.accounting.inventories.return_value = MagicMock(data=["pos1", "pos2"])
        result = gateway.get_positions()
        assert result == ["pos1", "pos2"]
        mock_sdk.accounting.inventories.assert_called_once()

    def test_returns_empty_on_none_result(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.accounting.inventories.return_value = None
        assert gateway.get_positions() == []

    def test_returns_empty_on_exception(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.accounting.inventories.side_effect = RuntimeError("network")
        assert gateway.get_positions() == []


# ---------------------------------------------------------------------------
# get_account_balance
# ---------------------------------------------------------------------------
class TestGetAccountBalance:
    def test_returns_settlement_data(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.accounting.query_settlement.return_value = MagicMock(
            data={"balance": 100_000}
        )
        result = gateway.get_account_balance()
        assert result == {"balance": 100_000}
        mock_sdk.accounting.query_settlement.assert_called_once()

    def test_returns_none_on_exception(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.accounting.query_settlement.side_effect = RuntimeError("timeout")
        assert gateway.get_account_balance() is None

    def test_uses_override_account(
        self,
        gateway: FubonAccountGateway,
        mock_sdk: MagicMock,
    ) -> None:
        alt_account = MagicMock(name="alt")
        mock_sdk.accounting.query_settlement.return_value = MagicMock(data="ok")
        gateway.get_account_balance(account=alt_account)
        mock_sdk.accounting.query_settlement.assert_called_once_with(alt_account, "0d")


# ---------------------------------------------------------------------------
# get_margin
# ---------------------------------------------------------------------------
class TestGetMargin:
    def test_returns_none_when_maintenance_missing(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        del mock_sdk.accounting.maintenance
        assert gateway.get_margin() is None

    def test_returns_margin_data(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.accounting.maintenance.return_value = MagicMock(data={"margin": 50})
        assert gateway.get_margin() == {"margin": 50}

    def test_returns_none_on_exception(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.accounting.maintenance.side_effect = RuntimeError("fail")
        assert gateway.get_margin() is None


# ---------------------------------------------------------------------------
# list_position_detail
# ---------------------------------------------------------------------------
class TestListPositionDetail:
    def test_returns_inventory_data(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.accounting.inventories.return_value = MagicMock(
            data=[{"symbol": "2330", "qty": 1}]
        )
        result = gateway.list_position_detail()
        assert result == [{"symbol": "2330", "qty": 1}]

    def test_returns_empty_on_exception(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.accounting.inventories.side_effect = RuntimeError("err")
        assert gateway.list_position_detail() == []


# ---------------------------------------------------------------------------
# list_profit_loss
# ---------------------------------------------------------------------------
class TestListProfitLoss:
    def test_returns_unrealized_pnl(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.accounting.unrealized_gains_and_loses.return_value = MagicMock(
            data=[{"pnl": 500}]
        )
        result = gateway.list_profit_loss()
        assert result == [{"pnl": 500}]

    def test_returns_empty_on_exception(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.accounting.unrealized_gains_and_loses.side_effect = RuntimeError("x")
        assert gateway.list_profit_loss() == []

    def test_uses_override_account(
        self,
        gateway: FubonAccountGateway,
        mock_sdk: MagicMock,
    ) -> None:
        alt = MagicMock(name="alt_acc")
        mock_sdk.accounting.unrealized_gains_and_loses.return_value = MagicMock(
            data=[]
        )
        gateway.list_profit_loss(account=alt)
        mock_sdk.accounting.unrealized_gains_and_loses.assert_called_once_with(alt)
