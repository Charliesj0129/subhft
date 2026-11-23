"""Tests for FubonOrderCodec, price formatting and SDK result decoding."""

from __future__ import annotations

import pytest

from tests.unit.fubon_mock_helper import (
    BSAction,
    FakeResult,
    FutOptMarketType,
    FutOptOrder,
    FutOptOrderType,
    FutOptPriceType,
    OrderType,
    PriceType,
    TimeInForce,
    fail,
    install_fubon_neo_mock,
    ok,
)

install_fubon_neo_mock()

from hft_platform.contracts.strategy import TIF, Side
from hft_platform.feed_adapter.fubon.order_codec import (
    FubonOrderAlreadyFilledError,
    FubonOrderCodec,
    FubonSdkResultError,
    encode_user_def,
    format_scaled_price,
    unwrap_sdk_result,
)


class TestEncodeSide:
    def test_buy_maps_to_real_bsaction(self) -> None:
        assert FubonOrderCodec().encode_side("Buy") is BSAction.Buy

    def test_sell_maps_to_real_bsaction(self) -> None:
        assert FubonOrderCodec().encode_side("Sell") is BSAction.Sell

    def test_platform_side_enum_is_accepted(self) -> None:
        codec = FubonOrderCodec()
        assert codec.encode_side(Side.BUY) is BSAction.Buy
        assert codec.encode_side(Side.SELL) is BSAction.Sell

    def test_invalid_side_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown side"):
            FubonOrderCodec().encode_side("Short")


class TestEncodeTif:
    @pytest.mark.parametrize(
        ("tif", "expected"),
        [("ROD", TimeInForce.ROD), ("IOC", TimeInForce.IOC), ("FOK", TimeInForce.FOK)],
    )
    def test_named_tif_maps_to_real_time_in_force(self, tif: str, expected: TimeInForce) -> None:
        assert FubonOrderCodec().encode_tif(tif) is expected

    def test_platform_tif_enum_is_accepted(self) -> None:
        codec = FubonOrderCodec()
        assert codec.encode_tif(TIF.LIMIT) is TimeInForce.ROD
        assert codec.encode_tif(TIF.IOC) is TimeInForce.IOC
        assert codec.encode_tif(TIF.FOK) is TimeInForce.FOK

    def test_invalid_tif_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown TIF"):
            FubonOrderCodec().encode_tif("GTC")


class TestEncodeStockTypes:
    def test_limit_and_market_price_type(self) -> None:
        codec = FubonOrderCodec()
        assert codec.encode_price_type("LMT") is PriceType.Limit
        assert codec.encode_price_type("MKT") is PriceType.Market

    def test_invalid_price_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown price type"):
            FubonOrderCodec().encode_price_type("STOP")

    def test_stock_order_types(self) -> None:
        codec = FubonOrderCodec()
        assert codec.encode_order_type("Stock") is OrderType.Stock
        assert codec.encode_order_type("DayTrade") is OrderType.DayTrade
        assert codec.encode_order_type("Margin") is OrderType.Margin

    def test_invalid_order_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown order type"):
            FubonOrderCodec().encode_order_type("Crypto")


class TestEncodeFutopt:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("LMT", FutOptPriceType.Limit),
            ("MKT", FutOptPriceType.Market),
            ("MKP", FutOptPriceType.RangeMarket),
        ],
    )
    def test_futopt_price_type_maps_to_real_enum(self, name: str, expected: FutOptPriceType) -> None:
        assert FubonOrderCodec().encode_futopt_price_type(name) is expected

    def test_invalid_futopt_price_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown price type"):
            FubonOrderCodec().encode_futopt_price_type("STOP")

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Auto", FutOptOrderType.Auto),
            ("New", FutOptOrderType.New),
            ("Close", FutOptOrderType.Close),
            ("FdayTrade", FutOptOrderType.FdayTrade),
        ],
    )
    def test_futopt_order_type_maps_to_real_enum(self, name: str, expected: FutOptOrderType) -> None:
        assert FubonOrderCodec().encode_futopt_order_type(name) is expected

    def test_stock_order_type_is_not_a_futopt_order_type(self) -> None:
        with pytest.raises(ValueError, match="Unknown futopt order type"):
            FubonOrderCodec().encode_futopt_order_type("Stock")

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Future", FutOptMarketType.Future),
            ("FutureNight", FutOptMarketType.FutureNight),
            ("Option", FutOptMarketType.Option),
            ("OptionNight", FutOptMarketType.OptionNight),
        ],
    )
    def test_futopt_market_type_maps_to_real_enum(self, name: str, expected: FutOptMarketType) -> None:
        assert FubonOrderCodec().encode_futopt_market_type(name) is expected

    def test_invalid_market_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown futopt market type"):
            FubonOrderCodec().encode_futopt_market_type("Stock")

    def test_build_futopt_order_uses_real_field_names(self) -> None:
        order = FubonOrderCodec().build_futopt_order(
            market_type=FutOptMarketType.Future,
            price_type=FutOptPriceType.Limit,
            time_in_force=TimeInForce.ROD,
            order_type=FutOptOrderType.Auto,
            buy_sell=BSAction.Buy,
            symbol="TMFK6",
            lot=2,
            price="22000.5",
            user_def="r47",
        )
        assert isinstance(order, FutOptOrder)
        assert (order.symbol, order.lot, order.price, order.user_def) == ("TMFK6", 2, "22000.5", "r47")
        assert order.buy_sell is BSAction.Buy
        assert order.market_type is FutOptMarketType.Future


class TestFormatScaledPrice:
    @pytest.mark.parametrize(
        ("scaled", "expected"),
        [
            (220005000, "22000.5"),
            (220000000, "22000"),
            (1234567, "123.4567"),
            (5, "0.0005"),
            (10000, "1"),
            (-15000, "-1.5"),
        ],
    )
    def test_scaled_int_is_rendered_exactly(self, scaled: int, expected: str) -> None:
        assert format_scaled_price(scaled) == expected

    def test_float_price_is_refused(self) -> None:
        with pytest.raises(TypeError, match="scaled int"):
            format_scaled_price(22000.5)

    def test_bool_price_is_refused(self) -> None:
        with pytest.raises(TypeError, match="scaled int"):
            format_scaled_price(True)


class TestEncodeUserDef:
    def test_none_stays_none(self) -> None:
        assert encode_user_def(None) is None

    def test_short_ascii_token_is_kept(self) -> None:
        assert encode_user_def("r47strat1") == "r47strat1"

    @pytest.mark.parametrize("bad", ["", "has space", "dash-ed", "x" * 11, "策略"])
    def test_invalid_user_def_raises(self, bad: str) -> None:
        with pytest.raises(ValueError, match="user_def"):
            encode_user_def(bad)


class TestUnwrapSdkResult:
    def test_success_returns_data(self) -> None:
        assert unwrap_sdk_result(ok([1, 2]), "op") == [1, 2]

    def test_failure_raises_with_message(self) -> None:
        with pytest.raises(FubonSdkResultError, match="op: SDK reported failure: nope"):
            unwrap_sdk_result(fail("nope"), "op")

    def test_empty_list_is_success_not_failure(self) -> None:
        assert unwrap_sdk_result(ok([]), "op") == []

    def test_non_result_object_raises(self) -> None:
        with pytest.raises(FubonSdkResultError, match="unexpected SDK result shape"):
            unwrap_sdk_result({"data": []}, "op")

    def test_none_raises(self) -> None:
        with pytest.raises(FubonSdkResultError, match="unexpected SDK result shape"):
            unwrap_sdk_result(None, "op")

    def test_truthy_non_true_flag_is_failure(self) -> None:
        with pytest.raises(FubonSdkResultError):
            unwrap_sdk_result(FakeResult(is_success="yes"), "op")


class TestCancelOfFilledOrder:
    """The broker's refusal to cancel a filled order, as the official test environment words it."""

    def test_cancel_of_a_filled_order_raises_the_already_filled_error(self) -> None:
        with pytest.raises(FubonOrderAlreadyFilledError):
            unwrap_sdk_result(fail("期貨委託目前狀態成交單已不允許取消交易"), "cancel_order")

    def test_already_filled_error_is_still_a_sdk_result_error(self) -> None:
        with pytest.raises(FubonSdkResultError, match="cancel_order: SDK reported failure"):
            unwrap_sdk_result(fail("期貨委託目前狀態成交單已不允許取消交易"), "cancel_order")

    def test_filled_marker_past_the_truncation_limit_is_still_recognised(self) -> None:
        with pytest.raises(FubonOrderAlreadyFilledError):
            unwrap_sdk_result(fail("x" * 300 + "期貨委託目前狀態成交單已不允許取消交易"), "cancel_order")

    @pytest.mark.parametrize("message", ["system busy", "order not found", "", "None"])
    def test_other_cancel_refusal_is_a_plain_sdk_result_error(self, message: str) -> None:
        with pytest.raises(FubonSdkResultError) as excinfo:
            unwrap_sdk_result(fail(message), "cancel_order")

        assert not isinstance(excinfo.value, FubonOrderAlreadyFilledError)

    @pytest.mark.parametrize("op", ["place_order", "modify_price", "convert_symbol"])
    def test_filled_wording_on_another_operation_is_not_a_cancel_no_op(self, op: str) -> None:
        with pytest.raises(FubonSdkResultError) as excinfo:
            unwrap_sdk_result(fail("期貨委託目前狀態成交單已不允許取消交易"), op)

        assert not isinstance(excinfo.value, FubonOrderAlreadyFilledError)
