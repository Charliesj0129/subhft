"""Tests for FubonAccountGateway BrokerProtocol-aligned methods."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.fubon.account_gateway import FubonAccountGateway


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture()
def mock_sdk() -> MagicMock:
    """Create a mock Fubon SDK with stock, accounting, and settlements."""
    sdk = MagicMock()
    sdk.stock.inventories.return_value = [
        {"symbol": "2330", "qty": 1000},
        {"symbol": "2317", "qty": 500},
    ]
    sdk.accounting.return_value = {"balance": 1_000_000, "margin_used": 200_000}
    sdk.futopt_accounting.return_value = {"margin": 500_000}
    sdk.settlements.return_value = [{"date": "2026-03-10", "amount": 50_000}]
    # unrealized_gains_and_loses on the accounting sub-object
    sdk.accounting.unrealized_gains_and_loses = MagicMock(
        return_value=[{"symbol": "2330", "pnl": 12000}]
    )
    sdk.accounting.query_settlement = MagicMock(
        return_value=[{"date": "2026-03-01", "amount": 30_000}]
    )
    return sdk


@pytest.fixture()
def gateway(mock_sdk: MagicMock) -> FubonAccountGateway:
    return FubonAccountGateway(sdk=mock_sdk, account="test-account")


# ------------------------------------------------------------------ #
# Existing low-level methods (sanity check)
# ------------------------------------------------------------------ #


class TestLowLevelMethods:
    def test_get_inventories(self, gateway: FubonAccountGateway, mock_sdk: MagicMock) -> None:
        result = gateway.get_inventories()
        assert len(result) == 2
        mock_sdk.stock.inventories.assert_called_once()

    def test_get_accounting(self, gateway: FubonAccountGateway, mock_sdk: MagicMock) -> None:
        result = gateway.get_accounting()
        assert result["balance"] == 1_000_000
        mock_sdk.accounting.assert_called_once()

    def test_get_margin(self, gateway: FubonAccountGateway, mock_sdk: MagicMock) -> None:
        result = gateway.get_margin()
        assert result["margin"] == 500_000
        mock_sdk.futopt_accounting.assert_called_once()

    def test_get_settlements(self, gateway: FubonAccountGateway, mock_sdk: MagicMock) -> None:
        result = gateway.get_settlements()
        assert len(result) == 1
        mock_sdk.settlements.assert_called_once()


# ------------------------------------------------------------------ #
# BrokerProtocol-aligned: get_positions
# ------------------------------------------------------------------ #


class TestGetPositions:
    def test_delegates_to_get_inventories(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        result = gateway.get_positions()
        assert len(result) == 2
        mock_sdk.stock.inventories.assert_called_once()

    def test_returns_empty_on_failure(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.stock.inventories.side_effect = RuntimeError("connection lost")
        result = gateway.get_positions()
        assert result == []


# ------------------------------------------------------------------ #
# BrokerProtocol-aligned: get_account_balance
# ------------------------------------------------------------------ #


class TestGetAccountBalance:
    def test_delegates_to_get_accounting(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        result = gateway.get_account_balance()
        assert result["balance"] == 1_000_000
        mock_sdk.accounting.assert_called_once()

    def test_account_param_ignored(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        """Account parameter is for protocol compat; Fubon SDK ignores it."""
        result = gateway.get_account_balance(account="some-account")
        assert result["balance"] == 1_000_000
        mock_sdk.accounting.assert_called_once()

    def test_returns_empty_dict_on_failure(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.accounting.side_effect = RuntimeError("timeout")
        result = gateway.get_account_balance()
        assert result == {}


# ------------------------------------------------------------------ #
# BrokerProtocol-aligned: list_position_detail
# ------------------------------------------------------------------ #


class TestListPositionDetail:
    def test_delegates_to_unrealized_gains(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        result = gateway.list_position_detail()
        assert len(result) == 1
        assert result[0]["symbol"] == "2330"
        mock_sdk.accounting.unrealized_gains_and_loses.assert_called_once_with(
            "test-account"
        )

    def test_uses_explicit_account(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        gateway.list_position_detail(account="other-account")
        mock_sdk.accounting.unrealized_gains_and_loses.assert_called_once_with(
            "other-account"
        )

    def test_returns_empty_when_sdk_method_missing(self, mock_sdk: MagicMock) -> None:
        del mock_sdk.accounting.unrealized_gains_and_loses
        gw = FubonAccountGateway(sdk=mock_sdk)
        result = gw.list_position_detail()
        assert result == []

    def test_returns_empty_on_exception(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.accounting.unrealized_gains_and_loses.side_effect = RuntimeError("err")
        result = gateway.list_position_detail()
        assert result == []

    def test_wraps_non_list_result(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        """If SDK returns a single object instead of a list, wrap it."""
        mock_sdk.accounting.unrealized_gains_and_loses.return_value = {"pnl": 100}
        result = gateway.list_position_detail()
        assert result == [{"pnl": 100}]


# ------------------------------------------------------------------ #
# BrokerProtocol-aligned: list_profit_loss
# ------------------------------------------------------------------ #


class TestListProfitLoss:
    def test_delegates_to_query_settlement(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        result = gateway.list_profit_loss(begin_date="2026-01-01", end_date="2026-03-01")
        assert len(result) == 1
        mock_sdk.accounting.query_settlement.assert_called_once_with(
            "test-account", begin_date="2026-01-01", end_date="2026-03-01"
        )

    def test_uses_explicit_account(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        gateway.list_profit_loss(account="other")
        mock_sdk.accounting.query_settlement.assert_called_once_with("other")

    def test_omits_none_dates(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        gateway.list_profit_loss()
        mock_sdk.accounting.query_settlement.assert_called_once_with("test-account")

    def test_returns_empty_when_sdk_method_missing(self, mock_sdk: MagicMock) -> None:
        del mock_sdk.accounting.query_settlement
        gw = FubonAccountGateway(sdk=mock_sdk)
        result = gw.list_profit_loss()
        assert result == []

    def test_returns_empty_on_exception(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.accounting.query_settlement.side_effect = RuntimeError("err")
        result = gateway.list_profit_loss()
        assert result == []

    def test_wraps_non_list_result(
        self, gateway: FubonAccountGateway, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.accounting.query_settlement.return_value = {"amount": 50}
        result = gateway.list_profit_loss()
        assert result == [{"amount": 50}]


# ------------------------------------------------------------------ #
# Constructor
# ------------------------------------------------------------------ #


class TestConstructor:
    def test_default_account_is_none(self, mock_sdk: MagicMock) -> None:
        gw = FubonAccountGateway(sdk=mock_sdk)
        assert gw._account is None

    def test_account_stored(self, mock_sdk: MagicMock) -> None:
        gw = FubonAccountGateway(sdk=mock_sdk, account="my-acct")
        assert gw._account == "my-acct"

    def test_slots(self, gateway: FubonAccountGateway) -> None:
        assert hasattr(gateway, "__slots__")
        assert "_account" in gateway.__slots__
