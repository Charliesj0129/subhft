"""Tests for FubonExecutionCallbackAdapter against the real fubon-neo 2.5.0 callback setters."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.fubon_mock_helper import (
    BSAction,
    CallPut,
    fail,
    install_fubon_neo_mock,
    make_convert_symbol,
    make_fubon_sdk_mock,
)

install_fubon_neo_mock()

from hft_platform.contracts.strategy import Side
from hft_platform.execution.field_map import get_field_map
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.feed_adapter.fubon import execution_callbacks as ec_module
from hft_platform.feed_adapter.fubon.execution_callbacks import (
    REQUIRED_SDK_SETTERS,
    FubonCallbackRegistrationError,
    FubonExecutionCallbackAdapter,
    FubonSymbolConversionError,
    FubonSymbolResolver,
    _resolve_order_id,
    _resolve_side,
    _scale_price,
    bridge_order_callback,
)

_NOW_NS = 1_700_000_000_000_000_000


def _order_report(**overrides: Any) -> SimpleNamespace:
    """A FutOptOrderResult-shaped report."""
    fields: dict[str, Any] = {
        "order_no": "l0001",
        "seq_no": "00000000001",
        "symbol": "FITX",
        "expiry_date": "202612",
        "strike_price": None,
        "call_put": None,
        "symbol_leg2": None,
        "buy_sell": BSAction.Buy,
        "price": 22000.0,
        "lot": 2,
        "status": 10,
        "function_type": 0,
        "user_def": "r47",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _fill_report(**overrides: Any) -> SimpleNamespace:
    """A FutOptFilledData-shaped report."""
    fields: dict[str, Any] = {
        "order_no": "l0001",
        "seq_no": "00000000001",
        "symbol": "FITX",
        "expiry_date": "202612",
        "strike_price": None,
        "call_put": None,
        "symbol_leg2": None,
        "buy_sell": BSAction.Sell,
        "filled_no": "00000000007",
        "filled_lots": 1,
        "filled_price": 20890.0,
        "filled_time": "10:31:00.931",
        "user_def": "r47",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture()
def sdk() -> MagicMock:
    return make_fubon_sdk_mock()


@pytest.fixture()
def orders() -> list[dict[str, Any]]:
    return []


@pytest.fixture()
def deals() -> list[dict[str, Any]]:
    return []


@pytest.fixture()
def adapter(sdk: MagicMock, orders: list[dict[str, Any]], deals: list[dict[str, Any]]) -> FubonExecutionCallbackAdapter:
    adapter = FubonExecutionCallbackAdapter(sdk)
    adapter.register(on_order=orders.append, on_deal=deals.append)
    return adapter


def _handler(sdk: MagicMock, setter: str) -> Any:
    """The callback the adapter handed to the named SDK setter."""
    return getattr(sdk, setter).call_args.args[0]


class TestRegistration:
    def test_register_wires_the_real_setters(self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter) -> None:
        for name in REQUIRED_SDK_SETTERS:
            getattr(sdk, name).assert_called_once()
            assert callable(_handler(sdk, name))

    def test_required_setters_are_the_ones_the_sdk_really_has(self) -> None:
        for name in REQUIRED_SDK_SETTERS:
            assert hasattr(make_fubon_sdk_mock(), name)

    @pytest.mark.parametrize("missing", REQUIRED_SDK_SETTERS)
    def test_missing_setter_raises_and_wires_nothing(self, sdk: MagicMock, missing: str) -> None:
        delattr(sdk, missing)
        adapter = FubonExecutionCallbackAdapter(sdk)

        with pytest.raises(FubonCallbackRegistrationError, match=missing):
            adapter.register(on_order=lambda p: None, on_deal=lambda p: None)

        for name in REQUIRED_SDK_SETTERS:
            if name != missing:
                getattr(sdk, name).assert_not_called()

    def test_sdk_with_only_the_old_wrong_names_is_refused(self) -> None:
        stale = SimpleNamespace(set_order_callback=lambda cb: None, set_on_dealt=lambda cb: None)
        adapter = FubonExecutionCallbackAdapter(stale)

        with pytest.raises(FubonCallbackRegistrationError) as excinfo:
            adapter.register(on_order=lambda p: None, on_deal=lambda p: None)

        for name in REQUIRED_SDK_SETTERS:
            assert name in str(excinfo.value)

    def test_non_callable_setter_counts_as_missing(self, sdk: MagicMock) -> None:
        sdk.set_on_event = None
        adapter = FubonExecutionCallbackAdapter(sdk)

        with pytest.raises(FubonCallbackRegistrationError, match="set_on_event"):
            adapter.register(on_order=lambda p: None, on_deal=lambda p: None)

        sdk.set_on_futopt_order.assert_not_called()

    @pytest.mark.parametrize("bad", [{"on_order": None}, {"on_deal": "nope"}])
    def test_non_callable_handler_raises(self, sdk: MagicMock, bad: dict[str, Any]) -> None:
        kwargs: dict[str, Any] = {"on_order": lambda p: None, "on_deal": lambda p: None}
        kwargs.update(bad)

        with pytest.raises(FubonCallbackRegistrationError, match="must be callable"):
            FubonExecutionCallbackAdapter(sdk).register(**kwargs)

        sdk.set_on_futopt_order.assert_not_called()

    def test_register_again_after_relogin_replaces_handlers(self, sdk: MagicMock) -> None:
        first: list[dict[str, Any]] = []
        second: list[dict[str, Any]] = []
        adapter = FubonExecutionCallbackAdapter(sdk)
        adapter.register(on_order=first.append, on_deal=lambda p: None)
        adapter.register(on_order=second.append, on_deal=lambda p: None)

        _handler(sdk, "set_on_futopt_order")("0", _order_report())

        assert sdk.set_on_futopt_order.call_count == 2
        assert first == []
        assert len(second) == 1


class TestOrderCallback:
    def test_order_report_is_translated_to_canonical_payload(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, orders: list[dict[str, Any]]
    ) -> None:
        with patch.object(ec_module.timebase, "now_ns", return_value=_NOW_NS):
            _handler(sdk, "set_on_futopt_order")("0", _order_report())

        assert orders == [
            {
                "order_id": "l0001",
                "order_no": "l0001",
                "seq_no": "00000000001",
                "symbol": "TXFL6",
                "side": "Buy",
                "action": "Buy",
                "price": 22000.0,
                "qty": 2,
                "status": "10",
                "function_type": 0,
                "op_type": "New",
                "source": "order",
                "strategy_id": "r47",
                "custom_field": "r47",
                "ts_ns": _NOW_NS,
                "order": {"price": 22000.0, "action": "Buy", "quantity": 2, "filled_qty": 0},
            }
        ]

    def test_price_is_passed_through_unscaled(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, orders: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_order")("0", _order_report(price=22000.5))

        assert orders[0]["price"] == 22000.5

    def test_single_argument_delivery_is_not_lost(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, orders: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_order")(_order_report())

        assert len(orders) == 1
        assert orders[0]["order_id"] == "l0001"

    def test_order_changed_report_uses_the_changed_setter_and_marks_the_source(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, orders: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_order_futopt_changed")("0", _order_report(function_type=30))

        assert orders[0]["source"] == "order_changed"
        assert orders[0]["op_type"] == "Cancel"

    @pytest.mark.parametrize(
        ("function_type", "expected"),
        [(0, "New"), (20, "UpdateQty"), (30, "Cancel"), ("30", "Cancel"), (15, ""), (None, ""), ("x", "")],
    )
    def test_only_documented_function_types_set_op_type(
        self,
        sdk: MagicMock,
        adapter: FubonExecutionCallbackAdapter,
        orders: list[dict[str, Any]],
        function_type: Any,
        expected: str,
    ) -> None:
        _handler(sdk, "set_on_futopt_order")("0", _order_report(function_type=function_type))

        assert orders[0]["op_type"] == expected

    def test_sell_side_enum_is_translated(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, orders: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_order")("0", _order_report(buy_sell=BSAction.Sell))

        assert (orders[0]["side"], orders[0]["action"]) == ("Sell", "Sell")

    def test_dict_report_is_accepted(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, orders: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_order")(
            "0",
            {"order_no": "d1", "symbol": "FITX", "expiry_date": "202612", "buy_sell": "Sell", "price": 1.0, "lot": 3},
        )

        assert (orders[0]["order_id"], orders[0]["symbol"], orders[0]["side"], orders[0]["qty"]) == (
            "d1",
            "TXFL6",
            "Sell",
            3,
        )

    def test_empty_content_is_dropped_and_counted(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, orders: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_order")("E123", None)

        assert orders == []
        assert adapter.error_count == 1

    def test_downstream_handler_error_is_counted_not_raised(self, sdk: MagicMock) -> None:
        def boom(payload: dict[str, Any]) -> None:
            raise RuntimeError("downstream down")

        adapter = FubonExecutionCallbackAdapter(sdk)
        adapter.register(on_order=boom, on_deal=lambda p: None)

        _handler(sdk, "set_on_futopt_order")("0", _order_report())

        assert adapter.error_count == 1

    def test_malformed_report_is_counted_not_raised(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, orders: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_order")("0", _order_report(lot="two"))

        assert orders == []
        assert adapter.error_count == 1

    def test_each_event_gets_an_independent_payload(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, orders: list[dict[str, Any]]
    ) -> None:
        handler = _handler(sdk, "set_on_futopt_order")
        handler("0", _order_report(order_no="a"))
        handler("0", _order_report(order_no="b"))

        assert [p["order_id"] for p in orders] == ["a", "b"]
        assert orders[0] is not orders[1]

    def test_no_handler_registered_is_a_noop(self, sdk: MagicMock) -> None:
        adapter = FubonExecutionCallbackAdapter(sdk)

        adapter._on_sdk_order("0", _order_report())

        assert adapter.error_count == 0

    def test_order_id_falls_back_to_seq_no(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, orders: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_order")("0", _order_report(order_no=None))

        assert orders[0]["order_id"] == "00000000001"


class TestDealCallback:
    def test_fill_report_is_translated_to_canonical_payload(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        with patch.object(ec_module.timebase, "now_ns", return_value=_NOW_NS):
            _handler(sdk, "set_on_futopt_filled")(None, _fill_report())

        assert deals == [
            {
                "order_id": "l0001",
                "order_no": "l0001",
                "seq_no": "00000000001:00000000007",
                "fill_id": "00000000001:00000000007",
                "symbol": "TXFL6",
                "side": "Sell",
                "action": "Sell",
                "price": 20890.0,
                "qty": 1,
                "strategy_id": "r47",
                "custom_field": "r47",
                "source_time": "10:31:00.931",
                "ts_ns": _NOW_NS,
            }
        ]

    def test_partial_fills_of_one_order_get_distinct_fill_ids(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        handler = _handler(sdk, "set_on_futopt_filled")
        handler(None, _fill_report(filled_no="00000000007"))
        handler(None, _fill_report(filled_no="00000000008"))

        assert deals[0]["seq_no"] != deals[1]["seq_no"]

    def test_redelivered_fill_keeps_the_same_fill_id(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        handler = _handler(sdk, "set_on_futopt_filled")
        handler(None, _fill_report())
        handler(None, _fill_report())

        assert deals[0]["fill_id"] == deals[1]["fill_id"]

    def test_fill_without_filled_no_falls_back_to_seq_no(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_filled")(None, _fill_report(filled_no=None))

        assert deals[0]["fill_id"] == "00000000001"

    def test_error_code_without_content_is_dropped_and_counted(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_filled")("recv error", None)

        assert deals == []
        assert adapter.error_count == 1

    def test_downstream_deal_handler_error_is_counted_not_raised(self, sdk: MagicMock) -> None:
        def boom(payload: dict[str, Any]) -> None:
            raise RuntimeError("downstream down")

        adapter = FubonExecutionCallbackAdapter(sdk)
        adapter.register(on_order=lambda p: None, on_deal=boom)

        _handler(sdk, "set_on_futopt_filled")(None, _fill_report())

        assert adapter.error_count == 1

    def test_no_handler_registered_is_a_noop(self, sdk: MagicMock) -> None:
        adapter = FubonExecutionCallbackAdapter(sdk)

        adapter._on_sdk_deal(None, _fill_report())

        assert adapter.error_count == 0


class TestEventCallback:
    def test_event_is_forwarded_to_the_handler(self, sdk: MagicMock) -> None:
        events: list[tuple[Any, Any]] = []
        adapter = FubonExecutionCallbackAdapter(sdk)
        adapter.register(on_order=lambda p: None, on_deal=lambda p: None, on_event=lambda c, x: events.append((c, x)))

        _handler(sdk, "set_on_event")("300", "disconnected")

        assert events == [("300", "disconnected")]

    def test_event_without_handler_does_not_raise(self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter) -> None:
        _handler(sdk, "set_on_event")("300", "disconnected")

        assert adapter.error_count == 0

    def test_event_handler_error_is_counted_not_raised(self, sdk: MagicMock) -> None:
        def boom(code: Any, content: Any) -> None:
            raise RuntimeError("bad handler")

        adapter = FubonExecutionCallbackAdapter(sdk)
        adapter.register(on_order=lambda p: None, on_deal=lambda p: None, on_event=boom)

        _handler(sdk, "set_on_event")("300", "x")

        assert adapter.error_count == 1


class TestBridge:
    def test_bridge_passes_the_source_as_the_platform_state(self) -> None:
        calls: list[tuple[Any, Any]] = []
        forward = bridge_order_callback(lambda state, payload: calls.append((state, payload)))

        forward({"source": "order_changed", "order_id": "x"})

        assert calls == [("order_changed", {"source": "order_changed", "order_id": "x"})]


class TestHelpers:
    def test_order_id_priority(self) -> None:
        assert _resolve_order_id({"order_no": "A", "ord_no": "B", "order_id": "C", "seq_no": "D"}) == "A"
        assert _resolve_order_id({"ord_no": "B", "order_id": "C", "seq_no": "D"}) == "B"
        assert _resolve_order_id({"order_id": "C", "seq_no": "D"}) == "C"
        assert _resolve_order_id({"seq_no": "D"}) == "D"
        assert _resolve_order_id({}) == ""

    def test_order_id_skips_blank_values(self) -> None:
        assert _resolve_order_id({"order_no": "  ", "seq_no": "D"}) == "D"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (BSAction.Buy, "Buy"),
            (BSAction.Sell, "Sell"),
            ("B", "Buy"),
            ("Buy", "Buy"),
            ("S", "Sell"),
            ("Sell", "Sell"),
            ("BSAction.Sell", "Sell"),
            ("X", "X"),
        ],
    )
    def test_side_translation(self, raw: Any, expected: str) -> None:
        assert _resolve_side({"buy_sell": raw}) == expected

    def test_scale_price_utility(self) -> None:
        assert _scale_price(580.5) == 5805000
        assert _scale_price(None) == 0
        assert _scale_price("abc") == 0


class TestNormalizerCompatibility:
    """The payload the adapter emits must survive ExecutionNormalizer unchanged in meaning."""

    @pytest.fixture()
    def normalizer(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> ExecutionNormalizer:
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text("symbols:\n  - code: 'TXFL6'\n    exchange: 'TAIFEX'\n    price_scale: 10000\n")
        monkeypatch.setenv("SYMBOLS_CONFIG", str(cfg))
        return ExecutionNormalizer(field_map=get_field_map("fubon"), default_account_id="ACCT-1")

    def test_sell_fill_is_not_read_as_a_buy(
        self,
        sdk: MagicMock,
        adapter: FubonExecutionCallbackAdapter,
        deals: list[dict[str, Any]],
        normalizer: ExecutionNormalizer,
    ) -> None:
        _handler(sdk, "set_on_futopt_filled")(None, _fill_report(buy_sell=BSAction.Sell, filled_lots=3))

        fill = normalizer.normalize_fill(RawExecEvent("deal", deals[0], time.time_ns()))

        assert fill is not None
        assert fill.side == Side.SELL
        assert fill.qty == 3
        assert fill.symbol == "TXFL6"
        assert fill.price == 208900000

    def test_float_fill_price_is_scaled_once_to_x10000_end_to_end(
        self,
        sdk: MagicMock,
        adapter: FubonExecutionCallbackAdapter,
        deals: list[dict[str, Any]],
        normalizer: ExecutionNormalizer,
    ) -> None:
        _handler(sdk, "set_on_futopt_filled")(None, _live_fill_report())

        fill = normalizer.normalize_fill(RawExecEvent("deal", deals[0], time.time_ns()))

        assert fill is not None
        assert deals[0]["price"] == 36128.0
        assert fill.price == 361280000
        assert (fill.symbol, fill.side, fill.qty) == ("TXFL6", Side.SELL, 1)
        assert fill.fill_id == "00111090102:00100000010"

    def test_fill_under_the_product_code_would_have_booked_the_wrong_symbol(
        self,
        sdk: MagicMock,
        adapter: FubonExecutionCallbackAdapter,
        deals: list[dict[str, Any]],
        normalizer: ExecutionNormalizer,
    ) -> None:
        _handler(sdk, "set_on_futopt_filled")(None, _live_fill_report())

        fill = normalizer.normalize_fill(RawExecEvent("deal", deals[0], time.time_ns()))

        assert fill is not None
        assert fill.symbol != "FITX"

    def test_order_event_keeps_price_side_and_quantity_through_the_normalizer(
        self,
        sdk: MagicMock,
        adapter: FubonExecutionCallbackAdapter,
        orders: list[dict[str, Any]],
        normalizer: ExecutionNormalizer,
    ) -> None:
        _handler(sdk, "set_on_futopt_order")("0", _live_order_report())

        event = normalizer.normalize_order(
            RawExecEvent("order", {"state": "order", "payload": orders[0]}, time.time_ns())
        )

        assert event is not None
        assert event.price == 359200000
        assert (event.symbol, event.side, event.submitted_qty, event.filled_qty) == ("TXFL6", Side.SELL, 1, 0)
        assert event.order_id == "C000J"

    def test_partial_fills_keep_distinct_fill_ids_through_the_normalizer(
        self,
        sdk: MagicMock,
        adapter: FubonExecutionCallbackAdapter,
        deals: list[dict[str, Any]],
        normalizer: ExecutionNormalizer,
    ) -> None:
        handler = _handler(sdk, "set_on_futopt_filled")
        handler(None, _fill_report(filled_no="00000000007"))
        handler(None, _fill_report(filled_no="00000000008"))

        fills = [normalizer.normalize_fill(RawExecEvent("deal", d, time.time_ns())) for d in deals]

        assert all(f is not None for f in fills)
        assert fills[0].fill_id != fills[1].fill_id


def _live_order_report() -> SimpleNamespace:
    """The order report the official test environment sent for a TXF sell (live-verified shape)."""
    return _order_report(
        order_no="C000J",
        seq_no="00111090102",
        price=35920.0,
        lot=1,
        filled_lot=0,
        function_type=10,
        user_def="hftsmk",
        buy_sell=BSAction.Sell,
    )


def _live_fill_report() -> SimpleNamespace:
    """The fill report the official test environment sent for that order (live-verified shape)."""
    return _fill_report(
        order_no="C000J",
        seq_no="00111090102",
        filled_no="00100000010",
        filled_lots=1,
        filled_price=36128.0,
        filled_time="10:57:08.460",
        user_def="hftsmk",
        buy_sell=BSAction.Sell,
    )


def _option_report(strike: float = 20000.0, call_put: Any = CallPut.Call, **overrides: Any) -> SimpleNamespace:
    return _order_report(symbol="TXO", strike_price=strike, call_put=call_put, **overrides)


class TestTradingSymbol:
    """Callbacks report the product code (FITX); the platform keys positions by the trading symbol (TXFL6)."""

    def test_order_report_carries_the_trading_symbol_not_the_product_code(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, orders: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_order")("0", _live_order_report())

        assert orders[0]["symbol"] == "TXFL6"

    def test_fill_report_carries_the_trading_symbol_not_the_product_code(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_filled")(None, _live_fill_report())

        assert deals[0]["symbol"] == "TXFL6"

    def test_order_changed_report_carries_the_trading_symbol(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, orders: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_order_futopt_changed")("0", _live_order_report())

        assert (orders[0]["source"], orders[0]["symbol"]) == ("order_changed", "TXFL6")

    def test_mini_product_maps_to_its_own_contract(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_filled")(None, _fill_report(symbol="FIMTX"))

        assert deals[0]["symbol"] == "MXFL6"

    def test_each_expiry_maps_to_its_own_contract(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        handler = _handler(sdk, "set_on_futopt_filled")
        handler(None, _fill_report(expiry_date="202612"))
        handler(None, _fill_report(expiry_date="202701", filled_no="00000000008"))

        assert [d["symbol"] for d in deals] == ["TXFL6", "TXFA7"]

    def test_option_conversion_uses_strike_and_call_put(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, orders: list[dict[str, Any]]
    ) -> None:
        handler = _handler(sdk, "set_on_futopt_order")
        handler("0", _option_report(call_put=CallPut.Call))
        handler("0", _option_report(call_put=CallPut.Put))
        handler("0", _option_report(strike=20100.0, call_put=CallPut.Call))

        assert [o["symbol"] for o in orders] == ["TXO20000L6", "TXO20000X6", "TXO20100L6"]
        sdk.futopt.convert_symbol.assert_any_call("TXO", "202612", 20000.0, CallPut.Put)

    def test_future_conversion_passes_only_product_and_expiry(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter
    ) -> None:
        _handler(sdk, "set_on_futopt_order")("0", _live_order_report())

        sdk.futopt.convert_symbol.assert_called_once_with("FITX", "202612")

    def test_dict_report_with_the_same_fields_converts_too(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        report = {"order_no": "d1", "seq_no": "1", "symbol": "FITX", "expiry_date": "202612", "filled_lots": 1}

        _handler(sdk, "set_on_futopt_filled")(None, report)

        assert deals[0]["symbol"] == "TXFL6"

    def test_conversion_is_asked_once_per_contract(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        handler = _handler(sdk, "set_on_futopt_filled")
        for number in range(5):
            handler(None, _fill_report(filled_no=f"{number:08d}"))

        assert [d["symbol"] for d in deals] == ["TXFL6"] * 5
        assert sdk.futopt.convert_symbol.call_count == 1

    def test_order_and_fill_of_one_contract_share_one_conversion(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter
    ) -> None:
        _handler(sdk, "set_on_futopt_order")("0", _live_order_report())
        _handler(sdk, "set_on_futopt_filled")(None, _live_fill_report())

        assert sdk.futopt.convert_symbol.call_count == 1

    def test_sdk_result_envelope_answer_is_unwrapped(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        sdk.futopt.convert_symbol.side_effect = None
        sdk.futopt.convert_symbol.return_value = SimpleNamespace(is_success=True, message=None, data="TXFL6")

        _handler(sdk, "set_on_futopt_filled")(None, _fill_report())

        assert deals[0]["symbol"] == "TXFL6"


class TestSymbolCache:
    def test_cache_holds_at_most_max_entries_and_evicts_the_oldest(self, sdk: MagicMock) -> None:
        resolver = FubonSymbolResolver(sdk, max_entries=2)

        resolver.resolve(_fill_report(expiry_date="202612"))
        resolver.resolve(_fill_report(expiry_date="202701"))
        resolver.resolve(_fill_report(symbol="FIMTX", expiry_date="202612"))

        assert len(resolver) == 2
        sdk.futopt.convert_symbol.reset_mock()
        resolver.resolve(_fill_report(expiry_date="202701"))
        resolver.resolve(_fill_report(symbol="FIMTX", expiry_date="202612"))
        assert sdk.futopt.convert_symbol.call_count == 0
        resolver.resolve(_fill_report(expiry_date="202612"))
        assert sdk.futopt.convert_symbol.call_count == 1

    def test_default_cache_is_bounded_under_many_distinct_strikes(self, sdk: MagicMock) -> None:
        table = {("TXO", "202612", float(strike), CallPut.Call): f"TXO{strike}L6" for strike in range(15000, 17000)}
        sdk.futopt.convert_symbol.side_effect = make_convert_symbol(table)
        resolver = FubonSymbolResolver(sdk)

        for strike in range(15000, 17000):
            resolver.resolve(_option_report(strike=float(strike)))

        assert 0 < len(resolver) <= 1024

    def test_failed_conversion_is_not_cached(self, sdk: MagicMock) -> None:
        resolver = FubonSymbolResolver(sdk)
        report = _fill_report(symbol="FITM")
        with pytest.raises(FubonSymbolConversionError):
            resolver.resolve(report)

        sdk.futopt.convert_symbol.side_effect = make_convert_symbol({("FITM", "202612"): "TMFL6"})

        assert resolver.resolve(report) == "TMFL6"

    @pytest.mark.parametrize("max_entries", [0, -1])
    def test_non_positive_capacity_is_refused(self, sdk: MagicMock, max_entries: int) -> None:
        with pytest.raises(ValueError, match="max_entries"):
            FubonSymbolResolver(sdk, max_entries=max_entries)


class TestUnresolvableSymbol:
    """A report whose symbol cannot be converted must never reach the platform under the wrong symbol."""

    def test_fill_with_an_unconvertible_product_is_not_forwarded(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_filled")(None, _fill_report(symbol="FITM"))

        assert deals == []

    def test_order_with_an_unconvertible_product_is_not_forwarded(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, orders: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_order")("0", _order_report(symbol="FITM"))

        assert orders == []

    def test_unresolved_report_is_counted_logged_and_recorded(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter
    ) -> None:
        adapter.log = MagicMock()
        registry = MagicMock()

        with patch.object(ec_module.MetricsRegistry, "get", return_value=registry):
            _handler(sdk, "set_on_futopt_filled")(None, _fill_report(symbol="FITM"))

        assert (adapter.symbol_error_count, adapter.error_count) == (1, 1)
        registry.execution_events_total.labels.assert_called_once_with(type="fubon_symbol_unresolved")
        registry.execution_events_total.labels.return_value.inc.assert_called_once_with()
        assert adapter.log.error.call_args.args[0] == "fubon_exec_cb.symbol_unresolved"
        assert adapter.unresolved_reports == (
            {
                "kind": "deal",
                "order_no": "l0001",
                "seq_no": "00000000001",
                "filled_no": "00000000007",
                "product": "FITM",
                "expiry_date": "202612",
            },
        )

    def test_metrics_failure_while_dropping_a_report_is_logged_not_raised(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        adapter.log = MagicMock()

        with patch.object(ec_module.MetricsRegistry, "get", side_effect=RuntimeError("registry down")):
            _handler(sdk, "set_on_futopt_filled")(None, _fill_report(symbol="FITM"))

        assert deals == []
        assert adapter.symbol_error_count == 1
        assert adapter.log.error.call_args.args[0] == "fubon_exec_cb.symbol_unresolved"
        assert adapter.log.warning.call_args.args[0] == "fubon_exec_cb.symbol_unresolved_metric_failed"

    def test_conversion_failure_does_not_stop_later_reports(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        handler = _handler(sdk, "set_on_futopt_filled")
        handler(None, _fill_report(symbol="FITM"))
        handler(None, _fill_report(filled_no="00000000008"))

        assert [d["symbol"] for d in deals] == ["TXFL6"]

    def test_report_without_expiry_is_not_forwarded(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_filled")(None, _fill_report(expiry_date=None))

        assert deals == []
        assert adapter.symbol_error_count == 1
        sdk.futopt.convert_symbol.assert_not_called()

    def test_report_without_a_product_symbol_is_not_forwarded(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_filled")(None, _fill_report(symbol=None))

        assert deals == []
        assert adapter.symbol_error_count == 1

    def test_option_report_without_a_strike_is_not_forwarded(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, orders: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_order")("0", _option_report(strike=None))

        assert orders == []
        assert adapter.symbol_error_count == 1
        sdk.futopt.convert_symbol.assert_not_called()

    def test_two_leg_spread_report_is_not_forwarded(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        _handler(sdk, "set_on_futopt_filled")(None, _fill_report(symbol_leg2="FITX"))

        assert deals == []
        assert adapter.symbol_error_count == 1

    def test_failed_result_envelope_is_not_forwarded(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]]
    ) -> None:
        sdk.futopt.convert_symbol.side_effect = None
        sdk.futopt.convert_symbol.return_value = fail("Symbol Name error")

        _handler(sdk, "set_on_futopt_filled")(None, _fill_report())

        assert deals == []
        assert adapter.symbol_error_count == 1

    @pytest.mark.parametrize("answer", [None, "", "  ", 42])
    def test_unusable_conversion_answer_is_not_forwarded(
        self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter, deals: list[dict[str, Any]], answer: Any
    ) -> None:
        sdk.futopt.convert_symbol.side_effect = None
        sdk.futopt.convert_symbol.return_value = answer

        _handler(sdk, "set_on_futopt_filled")(None, _fill_report())

        assert deals == []
        assert adapter.symbol_error_count == 1

    def test_sdk_without_convert_symbol_does_not_forward_the_product_code(
        self, sdk: MagicMock, orders: list[dict[str, Any]], deals: list[dict[str, Any]]
    ) -> None:
        del sdk.futopt.convert_symbol
        adapter = FubonExecutionCallbackAdapter(sdk)
        adapter.register(on_order=orders.append, on_deal=deals.append)

        _handler(sdk, "set_on_futopt_filled")(None, _fill_report())
        _handler(sdk, "set_on_futopt_order")("0", _order_report())

        assert (orders, deals) == ([], [])
        assert adapter.symbol_error_count == 2

    def test_unresolved_record_is_bounded(self, sdk: MagicMock, adapter: FubonExecutionCallbackAdapter) -> None:
        for number in range(ec_module._UNRESOLVED_KEEP + 10):
            _handler(sdk, "set_on_futopt_filled")(None, _fill_report(symbol="FITM", filled_no=str(number)))

        records = adapter.unresolved_reports
        assert len(records) == ec_module._UNRESOLVED_KEEP
        assert records[-1]["filled_no"] == str(ec_module._UNRESOLVED_KEEP + 9)
        assert adapter.symbol_error_count == ec_module._UNRESOLVED_KEEP + 10

    def test_downstream_failure_after_a_good_conversion_is_not_a_symbol_error(self, sdk: MagicMock) -> None:
        def boom(payload: dict[str, Any]) -> None:
            raise RuntimeError("downstream down")

        adapter = FubonExecutionCallbackAdapter(sdk)
        adapter.register(on_order=lambda p: None, on_deal=boom)

        _handler(sdk, "set_on_futopt_filled")(None, _fill_report())

        assert (adapter.error_count, adapter.symbol_error_count) == (1, 0)
        assert adapter.unresolved_reports == ()
