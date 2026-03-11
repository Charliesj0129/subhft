from __future__ import annotations

import pytest

from hft_platform.broker.order_translator import (
    FubonOrderTranslator,
    ShioajiOrderTranslator,
    TranslatedOrder,
)

SCALE = 10000


class TestShioajiOrderTranslator:
    def setup_method(self):
        self.t = ShioajiOrderTranslator()

    def test_shioaji_translate_buy(self):
        order = self.t.translate_new_order("2330", "buy", 185 * SCALE, 1, "ROD", "strat1", SCALE)
        assert order.action == "Buy"

    def test_shioaji_translate_sell(self):
        order = self.t.translate_new_order("2330", "sell", 185 * SCALE, 1, "ROD", "strat1", SCALE)
        assert order.action == "Sell"

    def test_shioaji_custom_field_truncated(self):
        order = self.t.translate_new_order("2330", "buy", 185 * SCALE, 1, "ROD", "abcdefghij", SCALE)
        assert order.custom_field == "abcdef"
        assert len(order.custom_field) == 6

    def test_shioaji_price_descaling(self):
        order = self.t.translate_new_order("2330", "buy", 185000, 1, "ROD", "s1", SCALE)
        assert order.price == pytest.approx(18.5)

    def test_shioaji_cancel(self):
        result = self.t.translate_cancel("ref123")
        assert result == {"trade": "ref123"}

    def test_shioaji_amend_price_and_qty(self):
        result = self.t.translate_amend("ref1", 200 * SCALE, 5, SCALE)
        assert result["trade"] == "ref1"
        assert result["price"] == pytest.approx(200.0)
        assert result["qty"] == 5

    def test_shioaji_amend_price_only(self):
        result = self.t.translate_amend("ref1", 200 * SCALE, None, SCALE)
        assert "price" in result
        assert "qty" not in result

    def test_shioaji_amend_qty_only(self):
        result = self.t.translate_amend("ref1", None, 10, SCALE)
        assert "price" not in result
        assert result["qty"] == 10

    def test_shioaji_max_custom_field_len(self):
        assert self.t.max_custom_field_len() == 6


class TestFubonOrderTranslator:
    def setup_method(self):
        self.t = FubonOrderTranslator()

    def test_fubon_translate_buy(self):
        order = self.t.translate_new_order("2330", "buy", 185 * SCALE, 1, "ROD", "strat1", SCALE)
        assert order.action == "B"

    def test_fubon_translate_sell(self):
        order = self.t.translate_new_order("2330", "sell", 185 * SCALE, 1, "ROD", "strat1", SCALE)
        assert order.action == "S"

    def test_fubon_order_type_limit(self):
        order = self.t.translate_new_order("2330", "buy", 185 * SCALE, 1, "ROD", "s1", SCALE)
        assert order.order_type == "L"

    def test_fubon_custom_field_32_chars(self):
        long_id = "a" * 40
        order = self.t.translate_new_order("2330", "buy", 185 * SCALE, 1, "ROD", long_id, SCALE)
        assert len(order.custom_field) == 32

    def test_fubon_user_def_in_extra(self):
        order = self.t.translate_new_order("2330", "buy", 185 * SCALE, 1, "ROD", "my_strat", SCALE)
        assert "user_def" in order.extra
        assert order.extra["user_def"] == "my_strat"

    def test_fubon_cancel(self):
        result = self.t.translate_cancel("order_abc")
        assert result == {"order_id": "order_abc"}

    def test_fubon_amend_price_and_qty(self):
        result = self.t.translate_amend("o1", 300 * SCALE, 10, SCALE)
        assert result["order_id"] == "o1"
        assert result["price"] == pytest.approx(300.0)
        assert result["quantity"] == 10

    def test_fubon_max_custom_field_len(self):
        assert self.t.max_custom_field_len() == 32


class TestValidation:
    @pytest.mark.parametrize("cls", [ShioajiOrderTranslator, FubonOrderTranslator])
    def test_validate_negative_qty(self, cls):
        t = cls()
        ok, reason = t.validate_pre_submit("2330", "buy", 100, -1)
        assert ok is False
        assert "quantity" in reason

    @pytest.mark.parametrize("cls", [ShioajiOrderTranslator, FubonOrderTranslator])
    def test_validate_zero_qty(self, cls):
        t = cls()
        ok, reason = t.validate_pre_submit("2330", "buy", 100, 0)
        assert ok is False

    @pytest.mark.parametrize("cls", [ShioajiOrderTranslator, FubonOrderTranslator])
    def test_validate_invalid_side(self, cls):
        t = cls()
        ok, reason = t.validate_pre_submit("2330", "short", 100, 1)
        assert ok is False
        assert "side" in reason

    @pytest.mark.parametrize("cls", [ShioajiOrderTranslator, FubonOrderTranslator])
    def test_validate_valid_order(self, cls):
        t = cls()
        ok, reason = t.validate_pre_submit("2330", "buy", 100, 1)
        assert ok is True
        assert reason == ""


class TestTranslatedOrder:
    def test_translated_order_frozen(self):
        order = TranslatedOrder(
            action="Buy",
            price=18.5,
            quantity=1,
            order_type="LMT",
            time_in_force="ROD",
            symbol="2330",
            custom_field="s1",
            extra={},
        )
        with pytest.raises(AttributeError):
            order.action = "Sell"  # type: ignore[misc]

    def test_cancel_shioaji_vs_fubon(self):
        sj = ShioajiOrderTranslator().translate_cancel("ref1")
        fb = FubonOrderTranslator().translate_cancel("ref1")
        assert "trade" in sj
        assert "order_id" in fb
        assert "trade" not in fb
        assert "order_id" not in sj
