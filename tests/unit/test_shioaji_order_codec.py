"""Tests for ShioajiOrderCodec."""

from __future__ import annotations

import pytest

from hft_platform.contracts.strategy import TIF, Side
from hft_platform.feed_adapter.shioaji.order_codec import ShioajiOrderCodec


@pytest.fixture()
def codec() -> ShioajiOrderCodec:
    return ShioajiOrderCodec()


# --- Side encoding ---


class TestEncodeSide:
    def test_buy(self, codec: ShioajiOrderCodec) -> None:
        assert codec.encode_side(Side.BUY) == "Buy"

    def test_sell(self, codec: ShioajiOrderCodec) -> None:
        assert codec.encode_side(Side.SELL) == "Sell"

    def test_invalid_raises(self, codec: ShioajiOrderCodec) -> None:
        with pytest.raises(ValueError, match="Unknown side"):
            codec.encode_side(999)  # type: ignore[arg-type]


# --- TIF encoding ---


class TestEncodeTif:
    def test_limit_maps_to_rod(self, codec: ShioajiOrderCodec) -> None:
        assert codec.encode_tif(TIF.LIMIT) == "ROD"

    def test_rod_maps_to_rod(self, codec: ShioajiOrderCodec) -> None:
        assert codec.encode_tif(TIF.ROD) == "ROD"

    def test_ioc(self, codec: ShioajiOrderCodec) -> None:
        assert codec.encode_tif(TIF.IOC) == "IOC"

    def test_fok(self, codec: ShioajiOrderCodec) -> None:
        assert codec.encode_tif(TIF.FOK) == "FOK"

    def test_invalid_raises(self, codec: ShioajiOrderCodec) -> None:
        with pytest.raises(ValueError, match="Unknown TIF"):
            codec.encode_tif(999)  # type: ignore[arg-type]


# --- Price type encoding ---


class TestEncodePriceType:
    def test_lmt(self, codec: ShioajiOrderCodec) -> None:
        assert codec.encode_price_type("LMT") == "LMT"

    def test_mkt(self, codec: ShioajiOrderCodec) -> None:
        assert codec.encode_price_type("MKT") == "MKT"

    def test_mkp(self, codec: ShioajiOrderCodec) -> None:
        assert codec.encode_price_type("MKP") == "MKP"

    def test_case_insensitive(self, codec: ShioajiOrderCodec) -> None:
        assert codec.encode_price_type("lmt") == "LMT"
        assert codec.encode_price_type("Mkt") == "MKT"

    def test_strips_whitespace(self, codec: ShioajiOrderCodec) -> None:
        assert codec.encode_price_type("  LMT  ") == "LMT"

    def test_invalid_raises(self, codec: ShioajiOrderCodec) -> None:
        with pytest.raises(ValueError, match="Unknown price_type"):
            codec.encode_price_type("INVALID")

    def test_empty_raises(self, codec: ShioajiOrderCodec) -> None:
        with pytest.raises(ValueError, match="Unknown price_type"):
            codec.encode_price_type("")


# --- Slots ---


class TestSlots:
    def test_has_slots(self) -> None:
        assert hasattr(ShioajiOrderCodec, "__slots__")

    def test_no_dict(self, codec: ShioajiOrderCodec) -> None:
        assert not hasattr(codec, "__dict__")
