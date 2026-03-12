"""Tests for FubonExecutionCallbackAdapter."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.fubon.execution_callbacks import (
    PRICE_SCALE,
    FubonExecutionCallbackAdapter,
    _resolve_order_id,
    _resolve_side,
    _scale_price,
)

# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture()
def sdk() -> MagicMock:
    """A mock Fubon SDK without callback-registration methods."""
    mock = MagicMock()
    # Remove SDK-level callback helpers so register() just stores them.
    del mock.set_order_callback
    del mock.set_on_dealt
    return mock


@pytest.fixture()
def adapter(sdk: MagicMock) -> FubonExecutionCallbackAdapter:
    return FubonExecutionCallbackAdapter(sdk)


# ------------------------------------------------------------------ #
# _resolve_order_id
# ------------------------------------------------------------------ #


class TestResolveOrderId:
    def test_prefers_ord_no(self) -> None:
        assert _resolve_order_id({"ord_no": "A1", "order_id": "B2", "seq_no": "C3"}) == "A1"

    def test_falls_back_to_order_id(self) -> None:
        assert _resolve_order_id({"order_id": "B2", "seq_no": "C3"}) == "B2"

    def test_falls_back_to_seq_no(self) -> None:
        assert _resolve_order_id({"seq_no": "C3"}) == "C3"

    def test_returns_empty_when_all_missing(self) -> None:
        assert _resolve_order_id({}) == ""

    def test_skips_none_values(self) -> None:
        assert _resolve_order_id({"ord_no": None, "order_id": "B2"}) == "B2"

    def test_skips_blank_string(self) -> None:
        assert _resolve_order_id({"ord_no": "  ", "order_id": "B2"}) == "B2"

    def test_works_with_object_style(self) -> None:
        obj = MagicMock()
        obj.ord_no = "X1"
        obj.order_id = "X2"
        obj.seq_no = "X3"
        assert _resolve_order_id(obj) == "X1"


# ------------------------------------------------------------------ #
# _resolve_side
# ------------------------------------------------------------------ #


class TestResolveSide:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("B", "Buy"),
            ("Buy", "Buy"),
            ("S", "Sell"),
            ("Sell", "Sell"),
        ],
    )
    def test_canonical_mapping(self, raw: str, expected: str) -> None:
        assert _resolve_side({"buy_sell": raw}) == expected

    def test_unknown_passes_through(self) -> None:
        assert _resolve_side({"buy_sell": "X"}) == "X"

    def test_missing_returns_empty(self) -> None:
        assert _resolve_side({}) == ""


# ------------------------------------------------------------------ #
# _scale_price
# ------------------------------------------------------------------ #


class TestScalePrice:
    def test_float_to_scaled_int(self) -> None:
        assert _scale_price(100.5) == 100_5000

    def test_string_numeric(self) -> None:
        assert _scale_price("50.25") == 50_2500

    def test_integer_input(self) -> None:
        assert _scale_price(10) == 10 * PRICE_SCALE

    def test_none_returns_zero(self) -> None:
        assert _scale_price(None) == 0

    def test_non_numeric_returns_zero(self) -> None:
        assert _scale_price("abc") == 0


# ------------------------------------------------------------------ #
# FubonExecutionCallbackAdapter — register
# ------------------------------------------------------------------ #


class TestRegister:
    def test_stores_callbacks(self, adapter: FubonExecutionCallbackAdapter) -> None:
        on_order = MagicMock()
        on_deal = MagicMock()
        adapter.register(on_order=on_order, on_deal=on_deal)
        assert adapter._on_order is on_order
        assert adapter._on_deal is on_deal

    def test_wires_sdk_callbacks_when_available(self) -> None:
        sdk = MagicMock()  # Has set_order_callback and set_on_dealt
        adapter = FubonExecutionCallbackAdapter(sdk)
        adapter.register(on_order=MagicMock(), on_deal=MagicMock())
        sdk.set_order_callback.assert_called_once_with(adapter._on_sdk_order)
        sdk.set_on_dealt.assert_called_once_with(adapter._on_sdk_deal)


# ------------------------------------------------------------------ #
# _on_sdk_order
# ------------------------------------------------------------------ #


class TestOnSdkOrder:
    def test_translates_dict_fields(self, adapter: FubonExecutionCallbackAdapter) -> None:
        received: list[dict[str, Any]] = []
        adapter.register(on_order=lambda d: received.append(dict(d)), on_deal=MagicMock())

        raw = {
            "ord_no": "ORD123",
            "stock_no": "2330",
            "buy_sell": "B",
            "price": "595.0",
            "qty": 1000,
            "status": "Filled",
            "user_def": "strat_alpha",
        }
        adapter._on_sdk_order(raw)

        assert len(received) == 1
        msg = received[0]
        assert msg["order_id"] == "ORD123"
        assert msg["symbol"] == "2330"
        assert msg["side"] == "Buy"
        assert msg["price"] == 595_0000
        assert msg["qty"] == 1000
        assert msg["status"] == "Filled"
        assert msg["strategy_id"] == "strat_alpha"
        assert msg["ts_ns"] > 0

    def test_sell_side(self, adapter: FubonExecutionCallbackAdapter) -> None:
        received: list[dict[str, Any]] = []
        adapter.register(on_order=lambda d: received.append(dict(d)), on_deal=MagicMock())

        adapter._on_sdk_order(
            {"ord_no": "X", "stock_no": "2317", "buy_sell": "S", "price": 100, "qty": 1, "status": "New"}
        )
        assert received[0]["side"] == "Sell"

    def test_no_callback_is_noop(self, adapter: FubonExecutionCallbackAdapter) -> None:
        # Should not raise when _on_order is None
        adapter._on_sdk_order({"ord_no": "X"})

    def test_handles_object_style_data(self, adapter: FubonExecutionCallbackAdapter) -> None:
        received: list[dict[str, Any]] = []
        adapter.register(on_order=lambda d: received.append(dict(d)), on_deal=MagicMock())

        obj = MagicMock()
        obj.ord_no = "OBJ1"
        obj.stock_no = "2454"
        obj.buy_sell = "Buy"
        obj.price = 800.0
        obj.qty = 5
        obj.status = "Pending"
        obj.user_def = "my_strat"
        adapter._on_sdk_order(obj)

        assert received[0]["order_id"] == "OBJ1"
        assert received[0]["symbol"] == "2454"
        assert received[0]["price"] == 800_0000

    def test_handles_missing_fields(self, adapter: FubonExecutionCallbackAdapter) -> None:
        received: list[dict[str, Any]] = []
        adapter.register(on_order=lambda d: received.append(dict(d)), on_deal=MagicMock())

        adapter._on_sdk_order({})
        msg = received[0]
        assert msg["order_id"] == ""
        assert msg["symbol"] == ""
        assert msg["price"] == 0
        assert msg["qty"] == 0


# ------------------------------------------------------------------ #
# _on_sdk_deal
# ------------------------------------------------------------------ #


class TestOnSdkDeal:
    def test_translates_deal_fields(self, adapter: FubonExecutionCallbackAdapter) -> None:
        received: list[dict[str, Any]] = []
        adapter.register(on_order=MagicMock(), on_deal=lambda d: received.append(dict(d)))

        raw = {
            "ord_no": "ORD456",
            "stock_no": "2330",
            "buy_sell": "S",
            "mat_price": "590.5",
            "mat_qty": 500,
            "user_def": "strat_beta",
        }
        adapter._on_sdk_deal(raw)

        assert len(received) == 1
        msg = received[0]
        assert msg["order_id"] == "ORD456"
        assert msg["symbol"] == "2330"
        assert msg["side"] == "Sell"
        assert msg["deal_price"] == 590_5000
        assert msg["deal_qty"] == 500
        assert msg["strategy_id"] == "strat_beta"
        assert msg["ts_ns"] > 0

    def test_no_callback_is_noop(self, adapter: FubonExecutionCallbackAdapter) -> None:
        adapter._on_sdk_deal({"ord_no": "X"})

    def test_handles_missing_fields(self, adapter: FubonExecutionCallbackAdapter) -> None:
        received: list[dict[str, Any]] = []
        adapter.register(on_order=MagicMock(), on_deal=lambda d: received.append(dict(d)))

        adapter._on_sdk_deal({})
        msg = received[0]
        assert msg["order_id"] == ""
        assert msg["deal_price"] == 0
        assert msg["deal_qty"] == 0


# ------------------------------------------------------------------ #
# Buffer reuse (Allocator Law)
# ------------------------------------------------------------------ #


class TestBufferReuse:
    def test_order_buffer_same_object(self, adapter: FubonExecutionCallbackAdapter) -> None:
        """The same dict object must be passed to the callback each time."""
        buffers: list[int] = []
        adapter.register(on_order=lambda d: buffers.append(id(d)), on_deal=MagicMock())

        adapter._on_sdk_order({"ord_no": "A", "stock_no": "1", "buy_sell": "B", "price": 10, "qty": 1, "status": "New"})
        adapter._on_sdk_order({"ord_no": "B", "stock_no": "2", "buy_sell": "S", "price": 20, "qty": 2, "status": "New"})

        assert len(buffers) == 2
        assert buffers[0] == buffers[1], "Order buffer must be reused (Allocator Law)"

    def test_deal_buffer_same_object(self, adapter: FubonExecutionCallbackAdapter) -> None:
        """The same dict object must be passed to the callback each time."""
        buffers: list[int] = []
        adapter.register(on_order=MagicMock(), on_deal=lambda d: buffers.append(id(d)))

        adapter._on_sdk_deal({"ord_no": "A", "stock_no": "1", "buy_sell": "B", "mat_price": 10, "mat_qty": 1})
        adapter._on_sdk_deal({"ord_no": "B", "stock_no": "2", "buy_sell": "S", "mat_price": 20, "mat_qty": 2})

        assert len(buffers) == 2
        assert buffers[0] == buffers[1], "Deal buffer must be reused (Allocator Law)"
