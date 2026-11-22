"""Tests for FubonAccountGateway against the real fubon-neo 2.5.0 futopt_accounting shapes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.fubon.account_gateway import FubonAccountGateway
from hft_platform.feed_adapter.fubon.order_codec import FubonSdkResultError
from tests.unit.fubon_mock_helper import (
    FakeResult,
    fail,
    make_account,
    make_fubon_sdk_mock,
    ok,
)


@pytest.fixture()
def sdk() -> MagicMock:
    return make_fubon_sdk_mock()


@pytest.fixture()
def account() -> SimpleNamespace:
    return make_account()


@pytest.fixture()
def gateway(sdk: MagicMock, account: SimpleNamespace) -> FubonAccountGateway:
    return FubonAccountGateway(sdk=sdk, account=account)


def _position(symbol: str = "TMFK6", lots: int = 1, pnl: float = 120.0) -> SimpleNamespace:
    return SimpleNamespace(symbol=symbol, tradable_lot=lots, profit_or_loss=pnl)


class TestLowLevelQueries:
    def test_get_margin_queries_margin_equity_with_the_account(
        self, gateway: FubonAccountGateway, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        equity = [SimpleNamespace(currency="NTD", available_margin=21453562.4)]
        sdk.futopt_accounting.query_margin_equity.return_value = ok(equity)

        result = gateway.get_margin()

        sdk.futopt_accounting.query_margin_equity.assert_called_once_with(account)
        assert result == equity

    def test_get_margin_failed_result_raises(self, gateway: FubonAccountGateway, sdk: MagicMock) -> None:
        sdk.futopt_accounting.query_margin_equity.return_value = fail("not logged in")

        with pytest.raises(FubonSdkResultError, match="not logged in"):
            gateway.get_margin()

    def test_get_single_positions_queries_single_position(
        self, gateway: FubonAccountGateway, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        positions = [_position()]
        sdk.futopt_accounting.query_single_position.return_value = ok(positions)

        assert gateway.get_single_positions() == positions
        sdk.futopt_accounting.query_single_position.assert_called_once_with(account)

    def test_get_hybrid_positions_queries_hybrid_position(
        self, gateway: FubonAccountGateway, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        hybrid = [SimpleNamespace(symbol="FITX", is_spread=False)]
        sdk.futopt_accounting.query_hybrid_position.return_value = ok(hybrid)

        assert gateway.get_hybrid_positions() == hybrid
        sdk.futopt_accounting.query_hybrid_position.assert_called_once_with(account)

    def test_query_without_account_raises_before_sdk(self, sdk: MagicMock) -> None:
        with pytest.raises(RuntimeError, match="no trading account"):
            FubonAccountGateway(sdk=sdk).get_margin()

        sdk.futopt_accounting.query_margin_equity.assert_not_called()

    def test_account_bound_after_login_is_used(self, sdk: MagicMock, account: SimpleNamespace) -> None:
        gateway = FubonAccountGateway(sdk=sdk)
        gateway.set_account(account)
        sdk.futopt_accounting.query_margin_equity.return_value = ok([])

        gateway.get_margin()

        sdk.futopt_accounting.query_margin_equity.assert_called_once_with(account)

    def test_non_list_data_is_not_read_as_an_empty_answer(self, gateway: FubonAccountGateway, sdk: MagicMock) -> None:
        sdk.futopt_accounting.query_single_position.return_value = FakeResult(True, data=None)

        with pytest.raises(FubonSdkResultError, match="expected a list"):
            gateway.get_single_positions()


class TestGetPositions:
    def test_returns_single_positions(self, gateway: FubonAccountGateway, sdk: MagicMock) -> None:
        positions = [_position("TMFK6", 2), _position("TXFK6", 1)]
        sdk.futopt_accounting.query_single_position.return_value = ok(positions)

        assert gateway.get_positions() == positions

    def test_confirmed_flat_book_is_an_empty_list_not_none(self, gateway: FubonAccountGateway, sdk: MagicMock) -> None:
        sdk.futopt_accounting.query_single_position.return_value = ok([])

        result = gateway.get_positions()

        assert result == []
        assert result is not None

    def test_failed_result_means_unknown_not_flat(self, gateway: FubonAccountGateway, sdk: MagicMock) -> None:
        sdk.futopt_accounting.query_single_position.return_value = fail("connection lost")

        assert gateway.get_positions() is None

    def test_sdk_exception_means_unknown_not_flat(self, gateway: FubonAccountGateway, sdk: MagicMock) -> None:
        sdk.futopt_accounting.query_single_position.side_effect = RuntimeError("connection lost")

        assert gateway.get_positions() is None

    def test_missing_data_means_unknown_not_flat(self, gateway: FubonAccountGateway, sdk: MagicMock) -> None:
        sdk.futopt_accounting.query_single_position.return_value = FakeResult(True, data=None)

        assert gateway.get_positions() is None

    def test_no_account_means_unknown_not_flat(self, sdk: MagicMock) -> None:
        assert FubonAccountGateway(sdk=sdk).get_positions() is None

        sdk.futopt_accounting.query_single_position.assert_not_called()


class TestGetAccountBalance:
    def test_returns_margin_equity(self, gateway: FubonAccountGateway, sdk: MagicMock) -> None:
        equity = [SimpleNamespace(currency="NTD", today_equity=22694910.4)]
        sdk.futopt_accounting.query_margin_equity.return_value = ok(equity)

        assert gateway.get_account_balance() == equity

    def test_returns_empty_dict_on_failure(self, gateway: FubonAccountGateway, sdk: MagicMock) -> None:
        sdk.futopt_accounting.query_margin_equity.side_effect = RuntimeError("timeout")

        assert gateway.get_account_balance() == {}

    def test_explicit_sdk_account_overrides_the_stored_one(self, gateway: FubonAccountGateway, sdk: MagicMock) -> None:
        other = make_account("7654321")
        sdk.futopt_accounting.query_margin_equity.return_value = ok([])

        gateway.get_account_balance(other)

        sdk.futopt_accounting.query_margin_equity.assert_called_once_with(other)

    def test_account_id_string_falls_back_to_the_stored_account(
        self, gateway: FubonAccountGateway, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        sdk.futopt_accounting.query_margin_equity.return_value = ok([])

        gateway.get_account_balance("1234567")

        sdk.futopt_accounting.query_margin_equity.assert_called_once_with(account)


class TestListPositionDetail:
    def test_returns_positions_with_unrealized_pnl(self, gateway: FubonAccountGateway, sdk: MagicMock) -> None:
        positions = [_position(pnl=63600.0)]
        sdk.futopt_accounting.query_single_position.return_value = ok(positions)

        result = gateway.list_position_detail()

        assert result == positions
        assert result[0].profit_or_loss == 63600.0

    def test_returns_empty_list_on_failure(self, gateway: FubonAccountGateway, sdk: MagicMock) -> None:
        sdk.futopt_accounting.query_single_position.return_value = fail("denied")

        assert gateway.list_position_detail() == []

    def test_explicit_account_is_used(self, gateway: FubonAccountGateway, sdk: MagicMock) -> None:
        other = make_account("7654321")
        sdk.futopt_accounting.query_single_position.return_value = ok([])

        gateway.list_position_detail(other)

        sdk.futopt_accounting.query_single_position.assert_called_once_with(other)


class TestListProfitLoss:
    def test_queries_close_position_record_with_both_dates(
        self, gateway: FubonAccountGateway, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        records = [SimpleNamespace(symbol="FITX", price=20847.0)]
        sdk.futopt_accounting.close_position_record.return_value = ok(records)

        result = gateway.list_profit_loss(begin_date="20260101", end_date="20260301")

        sdk.futopt_accounting.close_position_record.assert_called_once_with(account, "20260101", "20260301")
        assert result == records

    def test_omits_end_date_when_not_given(
        self, gateway: FubonAccountGateway, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        sdk.futopt_accounting.close_position_record.return_value = ok([])

        gateway.list_profit_loss(begin_date="20260101")

        sdk.futopt_accounting.close_position_record.assert_called_once_with(account, "20260101")

    def test_without_begin_date_returns_empty_without_calling_sdk(
        self, gateway: FubonAccountGateway, sdk: MagicMock
    ) -> None:
        assert gateway.list_profit_loss() == []

        sdk.futopt_accounting.close_position_record.assert_not_called()

    def test_returns_empty_list_on_failure(self, gateway: FubonAccountGateway, sdk: MagicMock) -> None:
        sdk.futopt_accounting.close_position_record.side_effect = RuntimeError("boom")

        assert gateway.list_profit_loss(begin_date="20260101") == []


class TestMockMatchesRealSdk:
    @pytest.mark.parametrize("name", ["inventories", "settlements", "unrealized_gains_and_loses", "query_settlement"])
    def test_stock_style_accounting_names_do_not_exist_on_the_mock(self, sdk: MagicMock, name: str) -> None:
        assert not hasattr(sdk.futopt_accounting, name)

    def test_query_without_account_argument_fails_the_test(self, sdk: MagicMock) -> None:
        with pytest.raises(TypeError):
            sdk.futopt_accounting.query_margin_equity()
