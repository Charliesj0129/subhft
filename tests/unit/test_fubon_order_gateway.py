"""Tests for FubonOrderGateway against the real fubon-neo 2.5.0 futopt shapes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.fubon_mock_helper import (
    BSAction,
    FutOptMarketType,
    FutOptOrder,
    FutOptOrderType,
    FutOptPriceType,
    TimeInForce,
    fail,
    install_fubon_neo_mock,
    make_account,
    make_fubon_sdk_mock,
    make_order_result,
    ok,
)

install_fubon_neo_mock()

from hft_platform.contracts.strategy import TIF, Side
from hft_platform.feed_adapter.fubon.order_codec import FubonOrderAlreadyFilledError, FubonSdkResultError
from hft_platform.feed_adapter.fubon.order_gateway import FubonCancelNoOp, FubonOrderGateway

# What the official test environment answers when a cancel loses the race against a fill.
_FILLED_CANCEL_MESSAGE = "期貨委託目前狀態成交單已不允許取消交易"


@pytest.fixture()
def sdk() -> MagicMock:
    return make_fubon_sdk_mock()


@pytest.fixture()
def account() -> SimpleNamespace:
    return make_account()


@pytest.fixture()
def gw(sdk: MagicMock, account: SimpleNamespace) -> FubonOrderGateway:
    return FubonOrderGateway(sdk, account=account)


def _sent_order(sdk: MagicMock) -> FutOptOrder:
    """The FutOptOrder the gateway handed to sdk.futopt.place_order."""
    order = sdk.futopt.place_order.call_args.args[1]
    assert isinstance(order, FutOptOrder)
    return order


class TestMockMatchesRealSdk:
    @pytest.mark.parametrize("name", ["place_futopt_order", "modify_order", "inventories", "settlements"])
    def test_method_the_real_sdk_lacks_does_not_exist_on_the_mock(self, sdk: MagicMock, name: str) -> None:
        assert not hasattr(sdk.futopt, name)

    def test_accounting_is_not_callable_on_the_mock(self, sdk: MagicMock) -> None:
        with pytest.raises(TypeError):
            sdk.futopt_accounting()

    def test_stale_stock_style_call_fails_the_test(self, sdk: MagicMock, account: SimpleNamespace) -> None:
        with pytest.raises(TypeError):
            sdk.futopt.place_order(buy_sell="Buy", symbol="TMFK6", price=1.0, quantity=1)

    def test_cancel_with_order_id_keyword_fails_the_test(self, sdk: MagicMock) -> None:
        with pytest.raises(TypeError):
            sdk.futopt.cancel_order(order_id="x")


class TestPlaceOrder:
    def test_place_order_builds_futopt_order_with_string_price(
        self, gw: FubonOrderGateway, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        sdk.futopt.place_order.return_value = ok(make_order_result())

        gw.place_order(symbol="TMFK6", price=220005000, qty=2, side="Buy")

        assert sdk.futopt.place_order.call_args.args[0] is account
        order = _sent_order(sdk)
        assert order.symbol == "TMFK6"
        assert order.lot == 2
        assert order.price == "22000.5"
        assert order.buy_sell is BSAction.Buy
        assert order.market_type is FutOptMarketType.Future
        assert order.price_type is FutOptPriceType.Limit
        assert order.time_in_force is TimeInForce.ROD
        assert order.order_type is FutOptOrderType.Auto
        assert order.user_def is None

    def test_place_order_returns_unwrapped_order_result(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        placed = make_order_result(order_no="l0042")
        sdk.futopt.place_order.return_value = ok(placed)

        assert gw.place_order(symbol="TMFK6", price=220000000, qty=1, side="Sell") is placed

    def test_place_order_sell_ioc_market_sends_no_price(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        sdk.futopt.place_order.return_value = ok(make_order_result())

        gw.place_order(symbol="TMFK6", price=0, qty=5, side="Sell", tif="IOC", price_type="MKT")

        order = _sent_order(sdk)
        assert order.buy_sell is BSAction.Sell
        assert order.time_in_force is TimeInForce.IOC
        assert order.price_type is FutOptPriceType.Market
        assert order.price is None

    def test_place_order_accepts_platform_enums(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        sdk.futopt.place_order.return_value = ok(make_order_result())

        gw.place_order(symbol="TMFK6", price=220000000, qty=1, side=Side.SELL, tif=TIF.IOC)

        order = _sent_order(sdk)
        assert order.buy_sell is BSAction.Sell
        assert order.time_in_force is TimeInForce.IOC

    def test_place_order_night_session_and_close_intent(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        sdk.futopt.place_order.return_value = ok(make_order_result())

        gw.place_order(
            symbol="TMFK6",
            price=220000000,
            qty=1,
            side="Buy",
            order_type="Close",
            market_type="FutureNight",
            user_def="r47",
        )

        order = _sent_order(sdk)
        assert order.market_type is FutOptMarketType.FutureNight
        assert order.order_type is FutOptOrderType.Close
        assert order.user_def == "r47"

    def test_place_order_failed_result_raises_instead_of_returning(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        sdk.futopt.place_order.return_value = fail("price out of range")

        with pytest.raises(FubonSdkResultError, match="price out of range"):
            gw.place_order(symbol="TMFK6", price=220000000, qty=1, side="Buy")

    def test_place_order_sdk_exception_propagates(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        sdk.futopt.place_order.side_effect = ConnectionError("timeout")

        with pytest.raises(ConnectionError, match="timeout"):
            gw.place_order(symbol="TMFK6", price=220000000, qty=1, side="Buy")

    def test_place_order_without_account_refuses_before_sdk(self, sdk: MagicMock) -> None:
        gw = FubonOrderGateway(sdk)

        with pytest.raises(RuntimeError, match="no trading account"):
            gw.place_order(symbol="TMFK6", price=220000000, qty=1, side="Buy")

        sdk.futopt.place_order.assert_not_called()

    def test_place_order_uses_account_bound_after_login(self, sdk: MagicMock, account: SimpleNamespace) -> None:
        gw = FubonOrderGateway(sdk)
        gw.set_account(account)
        sdk.futopt.place_order.return_value = ok(make_order_result())

        gw.place_order(symbol="TMFK6", price=220000000, qty=1, side="Buy")

        assert sdk.futopt.place_order.call_args.args[0] is account

    def test_place_order_without_sdk_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="place_order"):
            FubonOrderGateway(None).place_order(symbol="TMFK6", price=1, qty=1, side="Buy")

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"qty": 0},
            {"qty": -1},
            {"qty": 1.5},
            {"price": 0},
            {"price": None},
            {"price": 22000.5},
            {"side": "Short"},
            {"tif": "GTC"},
            {"price_type": "STOP"},
            {"order_type": "Stock"},
            {"market_type": "Stock"},
            {"user_def": "has space"},
            {"user_def": "toolongtoolong"},
        ],
    )
    def test_invalid_order_arguments_never_reach_sdk(
        self, gw: FubonOrderGateway, sdk: MagicMock, kwargs: dict[str, Any]
    ) -> None:
        args: dict[str, Any] = {"symbol": "TMFK6", "price": 220000000, "qty": 1, "side": "Buy"}
        args.update(kwargs)

        with pytest.raises((ValueError, TypeError)):
            gw.place_order(**args)

        sdk.futopt.place_order.assert_not_called()

    def test_missing_required_argument_raises_type_error(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        with pytest.raises(TypeError, match="symbol, qty, and side are required"):
            gw.place_order(symbol="TMFK6", price=220000000)

        sdk.futopt.place_order.assert_not_called()

    def test_place_futopt_order_alias_places_the_same_order(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        sdk.futopt.place_order.return_value = ok(make_order_result())

        gw.place_futopt_order(symbol="TXFA4", price=200000000, qty=2, side="Sell", tif="FOK")

        order = _sent_order(sdk)
        assert order.symbol == "TXFA4"
        assert order.price == "20000"
        assert order.time_in_force is TimeInForce.FOK


class TestCancelOrder:
    def test_cancel_order_passes_the_order_result_to_the_sdk(
        self, gw: FubonOrderGateway, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        placed = make_order_result(order_no="l0001")
        cancelled = make_order_result(order_no="l0001", function_type=30)
        sdk.futopt.cancel_order.return_value = ok(cancelled)

        result = gw.cancel_order(placed)

        sdk.futopt.cancel_order.assert_called_once_with(account, placed)
        sdk.futopt.get_order_results.assert_not_called()
        assert result is cancelled

    def test_cancel_order_by_number_looks_up_todays_order_results(
        self, gw: FubonOrderGateway, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        other = make_order_result(order_no="l0001", seq_no="s1")
        target = make_order_result(order_no="l0002", seq_no="s2")
        sdk.futopt.get_order_results.return_value = ok([other, target])
        sdk.futopt.cancel_order.return_value = ok(make_order_result(order_no="l0002"))

        gw.cancel_order("l0002")

        sdk.futopt.get_order_results.assert_called_once_with(account)
        sdk.futopt.cancel_order.assert_called_once_with(account, target)

    def test_cancel_order_by_order_id_attribute_looks_up_the_order(
        self, gw: FubonOrderGateway, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        target = make_order_result(order_no="l0003")
        sdk.futopt.get_order_results.return_value = ok([target])
        sdk.futopt.cancel_order.return_value = ok(target)

        gw.cancel_order(SimpleNamespace(order_id="l0003"))

        sdk.futopt.cancel_order.assert_called_once_with(account, target)

    def test_cancel_unknown_order_number_raises_without_cancelling(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        sdk.futopt.get_order_results.return_value = ok([make_order_result(order_no="l0001")])

        with pytest.raises(ValueError, match="not among today's order results"):
            gw.cancel_order("l9999")

        sdk.futopt.cancel_order.assert_not_called()

    def test_cancel_lookup_failure_raises_without_cancelling(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        sdk.futopt.get_order_results.return_value = fail("session expired")

        with pytest.raises(FubonSdkResultError, match="session expired"):
            gw.cancel_order("l0001")

        sdk.futopt.cancel_order.assert_not_called()

    def test_cancel_none_raises_type_error_before_sdk(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        with pytest.raises(TypeError, match="cannot extract an order identity"):
            gw.cancel_order(None)

        sdk.futopt.cancel_order.assert_not_called()

    def test_cancel_object_without_identity_raises_type_error(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        with pytest.raises(TypeError, match="cannot extract an order identity"):
            gw.cancel_order(SimpleNamespace())

        sdk.futopt.cancel_order.assert_not_called()

    def test_cancel_failed_result_raises(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        sdk.futopt.cancel_order.return_value = fail("system busy")

        with pytest.raises(FubonSdkResultError, match="system busy"):
            gw.cancel_order(make_order_result())

    def test_cancel_of_an_already_filled_order_is_a_terminal_no_op_not_an_error(
        self, gw: FubonOrderGateway, sdk: MagicMock
    ) -> None:
        sdk.futopt.cancel_order.return_value = fail(_FILLED_CANCEL_MESSAGE)

        result = gw.cancel_order(make_order_result(order_no="C000J"))

        assert result == FubonCancelNoOp(order_id="C000J", reason="already_filled")
        assert result is not None

    def test_cancel_of_an_already_filled_order_is_sent_once(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        sdk.futopt.cancel_order.return_value = fail(_FILLED_CANCEL_MESSAGE)

        gw.cancel_order(make_order_result(order_no="C000J"))

        sdk.futopt.cancel_order.assert_called_once()

    def test_cancel_of_an_already_filled_order_is_logged_as_info_not_error(
        self, gw: FubonOrderGateway, sdk: MagicMock
    ) -> None:
        sdk.futopt.cancel_order.return_value = fail(_FILLED_CANCEL_MESSAGE)
        gw.log = MagicMock()

        gw.cancel_order(make_order_result(order_no="C000J"))

        assert gw.log.info.call_args.args[0] == "fubon_cancel_order_already_filled"
        assert gw.log.info.call_args.kwargs["order_id"] == "C000J"
        gw.log.error.assert_not_called()

    def test_cancel_refusal_other_than_filled_is_still_an_error_with_an_error_log(
        self, gw: FubonOrderGateway, sdk: MagicMock
    ) -> None:
        sdk.futopt.cancel_order.return_value = fail("order not found")
        gw.log = MagicMock()

        with pytest.raises(FubonSdkResultError) as excinfo:
            gw.cancel_order(make_order_result(order_no="C000J"))

        assert not isinstance(excinfo.value, FubonOrderAlreadyFilledError)
        assert gw.log.error.call_args.args[0] == "fubon_cancel_order_failed"

    def test_cancel_sdk_exception_is_not_mistaken_for_an_already_filled_order(
        self, gw: FubonOrderGateway, sdk: MagicMock
    ) -> None:
        sdk.futopt.cancel_order.side_effect = RuntimeError(_FILLED_CANCEL_MESSAGE)

        with pytest.raises(RuntimeError, match="成交單"):
            gw.cancel_order(make_order_result())

    def test_cancel_sdk_exception_propagates(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        sdk.futopt.cancel_order.side_effect = RuntimeError("not found")

        with pytest.raises(RuntimeError, match="not found"):
            gw.cancel_order(make_order_result())

    def test_cancel_without_account_refuses_before_sdk(self, sdk: MagicMock) -> None:
        with pytest.raises(RuntimeError, match="no trading account"):
            FubonOrderGateway(sdk).cancel_order(make_order_result())

        sdk.futopt.cancel_order.assert_not_called()


class TestUpdateOrder:
    def test_price_change_uses_modify_price_obj(
        self, gw: FubonOrderGateway, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        placed = make_order_result()
        price_obj = object()
        modified = make_order_result(function_type=15)
        sdk.futopt.make_modify_price_obj.return_value = price_obj
        sdk.futopt.modify_price.return_value = ok(modified)

        result = gw.update_order(placed, price=220010000)

        sdk.futopt.make_modify_price_obj.assert_called_once_with(placed, "22001")
        sdk.futopt.modify_price.assert_called_once_with(account, price_obj)
        sdk.futopt.make_modify_lot_obj.assert_not_called()
        assert result is modified

    def test_lot_change_uses_modify_lot_obj(
        self, gw: FubonOrderGateway, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        placed = make_order_result()
        lot_obj = object()
        modified = make_order_result(function_type=20)
        sdk.futopt.make_modify_lot_obj.return_value = lot_obj
        sdk.futopt.modify_lot.return_value = ok(modified)

        result = gw.update_order(placed, qty=3)

        sdk.futopt.make_modify_lot_obj.assert_called_once_with(placed, 3)
        sdk.futopt.modify_lot.assert_called_once_with(account, lot_obj)
        sdk.futopt.make_modify_price_obj.assert_not_called()
        assert result is modified

    def test_price_and_lot_change_chains_the_result_of_the_first_step(
        self, gw: FubonOrderGateway, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        placed = make_order_result(order_no="l0001")
        after_price = make_order_result(order_no="l0001", function_type=15)
        after_lot = make_order_result(order_no="l0001", function_type=20)
        sdk.futopt.make_modify_price_obj.return_value = "price-obj"
        sdk.futopt.modify_price.return_value = ok(after_price)
        sdk.futopt.make_modify_lot_obj.return_value = "lot-obj"
        sdk.futopt.modify_lot.return_value = ok(after_lot)

        result = gw.update_order(placed, price=220010000, qty=1)

        sdk.futopt.make_modify_lot_obj.assert_called_once_with(after_price, 1)
        assert result is after_lot

    def test_update_by_order_number_looks_up_the_order(
        self, gw: FubonOrderGateway, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        target = make_order_result(order_no="l0002")
        sdk.futopt.get_order_results.return_value = ok([target])
        sdk.futopt.make_modify_price_obj.return_value = "price-obj"
        sdk.futopt.modify_price.return_value = ok(target)

        gw.update_order("l0002", price=220010000)

        sdk.futopt.make_modify_price_obj.assert_called_once_with(target, "22001")

    def test_update_without_changes_is_a_noop_returning_none(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        assert gw.update_order(make_order_result()) is None

        sdk.futopt.modify_price.assert_not_called()
        sdk.futopt.modify_lot.assert_not_called()

    def test_failed_price_modification_raises_and_skips_the_lot_step(
        self, gw: FubonOrderGateway, sdk: MagicMock
    ) -> None:
        sdk.futopt.make_modify_price_obj.return_value = "price-obj"
        sdk.futopt.modify_price.return_value = fail("not modifiable")

        with pytest.raises(FubonSdkResultError, match="not modifiable"):
            gw.update_order(make_order_result(), price=220010000, qty=1)

        sdk.futopt.modify_lot.assert_not_called()

    def test_failed_lot_step_after_price_step_names_the_step(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        sdk.futopt.make_modify_price_obj.return_value = "price-obj"
        sdk.futopt.modify_price.return_value = ok(make_order_result())
        sdk.futopt.make_modify_lot_obj.return_value = "lot-obj"
        sdk.futopt.modify_lot.return_value = fail("lot too large")

        with pytest.raises(FubonSdkResultError, match="modify_lot: SDK reported failure: lot too large"):
            gw.update_order(make_order_result(), price=220010000, qty=9)

        sdk.futopt.modify_price.assert_called_once()

    @pytest.mark.parametrize("kwargs", [{"price": 0}, {"price": -1}, {"price": 1.5}, {"qty": 0}, {"qty": -2}])
    def test_invalid_modification_never_reaches_sdk(
        self, gw: FubonOrderGateway, sdk: MagicMock, kwargs: dict[str, Any]
    ) -> None:
        with pytest.raises(ValueError, match="must be a positive"):
            gw.update_order(make_order_result(), **kwargs)

        sdk.futopt.make_modify_price_obj.assert_not_called()
        sdk.futopt.make_modify_lot_obj.assert_not_called()

    def test_update_none_trade_raises_type_error(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        with pytest.raises(TypeError, match="cannot extract an order identity"):
            gw.update_order(None, price=220010000)

        sdk.futopt.modify_price.assert_not_called()

    def test_update_sdk_exception_propagates(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        sdk.futopt.make_modify_price_obj.return_value = "price-obj"
        sdk.futopt.modify_price.side_effect = ValueError("invalid price")

        with pytest.raises(ValueError, match="invalid price"):
            gw.update_order(make_order_result(), price=220010000)


class TestBatchPlaceOrders:
    @staticmethod
    def _order(symbol: str = "TMFK6", price: int = 220000000, qty: int = 1, side: str = "Buy") -> dict[str, Any]:
        return {"symbol": symbol, "price": price, "qty": qty, "side": side}

    @patch("hft_platform.feed_adapter.fubon.order_gateway.time.sleep")
    def test_batch_all_succeed(self, mock_sleep: MagicMock, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        placed = [make_order_result(order_no=f"l000{i}") for i in range(3)]
        sdk.futopt.place_order.side_effect = [ok(p) for p in placed]

        results = gw.batch_place_orders([self._order(), self._order(side="Sell", qty=2), self._order(qty=3)])

        assert results == placed
        assert sdk.futopt.place_order.call_count == 3

    @patch("hft_platform.feed_adapter.fubon.order_gateway.time.sleep")
    def test_batch_partial_failure_isolates_the_failed_order(
        self, mock_sleep: MagicMock, gw: FubonOrderGateway, sdk: MagicMock
    ) -> None:
        first = make_order_result(order_no="l0001")
        third = make_order_result(order_no="l0003")
        sdk.futopt.place_order.side_effect = [ok(first), fail("rejected"), ok(third)]

        results = gw.batch_place_orders([self._order(), self._order(), self._order()])

        assert results == [first, None, third]

    def test_batch_empty_returns_empty(self, gw: FubonOrderGateway, sdk: MagicMock) -> None:
        assert gw.batch_place_orders([]) == []

        sdk.futopt.place_order.assert_not_called()

    @patch("hft_platform.feed_adapter.fubon.order_gateway.time.sleep")
    def test_batch_single_order_does_not_sleep(
        self, mock_sleep: MagicMock, gw: FubonOrderGateway, sdk: MagicMock
    ) -> None:
        single = make_order_result(order_no="l0001")
        sdk.futopt.place_order.return_value = ok(single)

        results = gw.batch_place_orders([self._order()])

        assert results == [single]
        mock_sleep.assert_not_called()

    @patch("hft_platform.feed_adapter.fubon.order_gateway.time.sleep")
    def test_batch_rate_limit_delay_between_orders(
        self, mock_sleep: MagicMock, gw: FubonOrderGateway, sdk: MagicMock
    ) -> None:
        sdk.futopt.place_order.return_value = ok(make_order_result())

        gw.batch_place_orders([self._order() for _ in range(3)])

        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(0.067)

    @patch("hft_platform.feed_adapter.fubon.order_gateway.time.sleep")
    def test_batch_forwards_market_type_and_user_def(
        self, mock_sleep: MagicMock, gw: FubonOrderGateway, sdk: MagicMock
    ) -> None:
        sdk.futopt.place_order.return_value = ok(make_order_result())

        gw.batch_place_orders([{**self._order(), "market_type": "FutureNight", "user_def": "b1"}])

        order = _sent_order(sdk)
        assert order.market_type is FutOptMarketType.FutureNight
        assert order.user_def == "b1"
