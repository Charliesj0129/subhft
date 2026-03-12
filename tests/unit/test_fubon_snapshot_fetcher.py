"""Tests for FubonSnapshotFetcher."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from hft_platform.feed_adapter.fubon.snapshot_fetcher import (
    PRICE_SCALE,
    FubonSnapshotFetcher,
    _scale_price,
)

# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


def _make_sdk(responses: dict[str, dict[str, Any]]) -> MagicMock:
    """Build a mock SDK where quote(symbol=X) returns responses[X]."""
    sdk = MagicMock()

    def _quote(symbol: str) -> dict[str, Any]:
        if symbol not in responses:
            raise RuntimeError(f"unknown symbol {symbol}")
        return responses[symbol]

    sdk.marketdata.rest_client.stock.intraday.quote.side_effect = _quote
    return sdk


def _make_quote_data(
    *,
    close: float = 100.5,
    volume: int = 1234,
    bid_price: float = 100.0,
    ask_price: float = 101.0,
    open_: float = 99.0,
    high: float = 102.0,
    low: float = 98.0,
) -> dict[str, Any]:
    return {
        "close": close,
        "volume": volume,
        "bid_price": bid_price,
        "ask_price": ask_price,
        "open": open_,
        "high": high,
        "low": low,
    }


# ------------------------------------------------------------------ #
# _scale_price tests
# ------------------------------------------------------------------ #


class TestScalePrice:
    def test_float(self) -> None:
        assert _scale_price(100.5) == 1_005_000

    def test_int(self) -> None:
        assert _scale_price(200) == 200 * PRICE_SCALE

    def test_str_numeric(self) -> None:
        assert _scale_price("50.25") == 502_500

    def test_none_returns_zero(self) -> None:
        assert _scale_price(None) == 0

    def test_unparseable_str_returns_zero(self) -> None:
        assert _scale_price("N/A") == 0

    def test_zero(self) -> None:
        assert _scale_price(0) == 0

    def test_negative(self) -> None:
        assert _scale_price(-10.0) == -100_000


# ------------------------------------------------------------------ #
# FubonSnapshotFetcher tests
# ------------------------------------------------------------------ #


class TestFetchSnapshots:
    def test_valid_single_symbol(self) -> None:
        sdk = _make_sdk({"2330": _make_quote_data()})
        fetcher = FubonSnapshotFetcher(sdk)
        result = fetcher.fetch_snapshots(["2330"])

        assert len(result) == 1
        snap = result[0]
        assert snap["code"] == "2330"
        assert snap["close"] == 1_005_000
        assert snap["volume"] == 1234
        assert snap["bid_price"] == 1_000_000
        assert snap["ask_price"] == 1_010_000
        assert snap["open"] == 990_000
        assert snap["high"] == 1_020_000
        assert snap["low"] == 980_000
        assert isinstance(snap["ts"], int)
        assert snap["ts"] > 0

    def test_multiple_symbols(self) -> None:
        sdk = _make_sdk(
            {
                "2330": _make_quote_data(close=100.0),
                "2317": _make_quote_data(close=200.0),
            }
        )
        fetcher = FubonSnapshotFetcher(sdk)
        result = fetcher.fetch_snapshots(["2330", "2317"])

        assert len(result) == 2
        assert result[0]["code"] == "2330"
        assert result[0]["close"] == 1_000_000
        assert result[1]["code"] == "2317"
        assert result[1]["close"] == 2_000_000

    def test_empty_symbols_returns_empty(self) -> None:
        sdk = _make_sdk({})
        fetcher = FubonSnapshotFetcher(sdk)
        assert fetcher.fetch_snapshots([]) == []

    def test_partial_failure_skips_failed(self) -> None:
        """Two symbols: first succeeds, second raises. Only first returned."""
        sdk = _make_sdk({"2330": _make_quote_data()})
        # "9999" is not in responses dict → raises RuntimeError
        fetcher = FubonSnapshotFetcher(sdk)
        result = fetcher.fetch_snapshots(["2330", "9999"])

        assert len(result) == 1
        assert result[0]["code"] == "2330"

    def test_all_fail_returns_empty(self) -> None:
        sdk = _make_sdk({})
        fetcher = FubonSnapshotFetcher(sdk)
        result = fetcher.fetch_snapshots(["BAD1", "BAD2"])
        assert result == []

    def test_missing_fields_default_to_zero(self) -> None:
        """SDK response with missing keys should default to 0."""
        sdk = _make_sdk({"2330": {}})  # empty dict
        fetcher = FubonSnapshotFetcher(sdk)
        result = fetcher.fetch_snapshots(["2330"])

        assert len(result) == 1
        snap = result[0]
        assert snap["code"] == "2330"
        assert snap["close"] == 0
        assert snap["volume"] == 0
        assert snap["bid_price"] == 0
        assert snap["ask_price"] == 0
        assert snap["open"] == 0
        assert snap["high"] == 0
        assert snap["low"] == 0

    def test_object_style_response(self) -> None:
        """SDK may return an object with attributes instead of a dict."""
        resp = SimpleNamespace(
            close=150.0,
            volume=500,
            bid_price=149.5,
            ask_price=150.5,
            open=148.0,
            high=151.0,
            low=147.0,
        )
        sdk = MagicMock()
        sdk.marketdata.rest_client.stock.intraday.quote.return_value = resp
        fetcher = FubonSnapshotFetcher(sdk)
        result = fetcher.fetch_snapshots(["2330"])

        assert len(result) == 1
        assert result[0]["close"] == 1_500_000
        assert result[0]["bid_price"] == 1_495_000
        assert result[0]["ask_price"] == 1_505_000

    def test_none_price_fields(self) -> None:
        """SDK response with None values should default to 0."""
        data = {
            "close": None,
            "volume": None,
            "bid_price": None,
            "ask_price": None,
            "open": None,
            "high": None,
            "low": None,
        }
        sdk = _make_sdk({"2330": data})
        fetcher = FubonSnapshotFetcher(sdk)
        result = fetcher.fetch_snapshots(["2330"])

        snap = result[0]
        assert snap["close"] == 0
        assert snap["volume"] == 0
        assert snap["bid_price"] == 0
