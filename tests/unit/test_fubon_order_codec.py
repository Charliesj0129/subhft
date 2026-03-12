"""Tests for FubonOrderCodec."""

from __future__ import annotations

import pytest

from tests.unit.fubon_mock_helper import install_fubon_neo_mock

install_fubon_neo_mock()

from hft_platform.feed_adapter.fubon.order_codec import FubonOrderCodec


class TestEncodeSide:
    def test_buy(self) -> None:
        codec = FubonOrderCodec()
        assert codec.encode_side("Buy") == "FUBON_BUY"

    def test_sell(self) -> None:
        codec = FubonOrderCodec()
        assert codec.encode_side("Sell") == "FUBON_SELL"

    def test_invalid_side_raises(self) -> None:
        codec = FubonOrderCodec()
        with pytest.raises(ValueError, match="Unknown side"):
            codec.encode_side("Short")


class TestEncodeTif:
    def test_rod(self) -> None:
        codec = FubonOrderCodec()
        assert codec.encode_tif("ROD") == "FUBON_ROD"

    def test_ioc(self) -> None:
        codec = FubonOrderCodec()
        assert codec.encode_tif("IOC") == "FUBON_IOC"

    def test_fok(self) -> None:
        codec = FubonOrderCodec()
        assert codec.encode_tif("FOK") == "FUBON_FOK"

    def test_invalid_tif_raises(self) -> None:
        codec = FubonOrderCodec()
        with pytest.raises(ValueError, match="Unknown TIF"):
            codec.encode_tif("GTC")


class TestEncodePriceType:
    def test_limit(self) -> None:
        codec = FubonOrderCodec()
        assert codec.encode_price_type("LMT") == "FUBON_LIMIT"

    def test_market(self) -> None:
        codec = FubonOrderCodec()
        assert codec.encode_price_type("MKT") == "FUBON_MARKET"

    def test_invalid_price_type_raises(self) -> None:
        codec = FubonOrderCodec()
        with pytest.raises(ValueError, match="Unknown price type"):
            codec.encode_price_type("STOP")


class TestEncodeOrderType:
    def test_stock(self) -> None:
        codec = FubonOrderCodec()
        assert codec.encode_order_type("Stock") == "FUBON_STOCK"

    def test_daytrade(self) -> None:
        codec = FubonOrderCodec()
        assert codec.encode_order_type("DayTrade") == "FUBON_DAYTRADE"

    def test_margin(self) -> None:
        codec = FubonOrderCodec()
        assert codec.encode_order_type("Margin") == "FUBON_MARGIN"

    def test_invalid_order_type_raises(self) -> None:
        codec = FubonOrderCodec()
        with pytest.raises(ValueError, match="Unknown order type"):
            codec.encode_order_type("Crypto")
