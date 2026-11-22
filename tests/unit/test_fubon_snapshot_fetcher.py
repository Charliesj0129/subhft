"""Tests for FubonSnapshotFetcher against the documented futopt ``intraday/quote`` response."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.events import BidAskEvent
from hft_platform.feed_adapter.fubon import snapshot_fetcher
from hft_platform.feed_adapter.fubon.snapshot_fetcher import (
    FubonSnapshotFetcher,
    _coerce_int,
    _coerce_price,
)
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

# Response body of the official ``intraday/quote`` example (futures/options market data
# HTTP API), verbatim apart from the Python literal syntax.  Prices are plain numbers and
# every time is epoch microseconds.
DOCUMENTED_QUOTE: dict[str, Any] = {
    "date": "2023-12-12",
    "type": "FUTURE",
    "exchange": "TAIFEX",
    "symbol": "TXFA4",
    "name": "臺股期貨014",
    "previousClose": 17416,
    "openPrice": 17514,
    "openTime": 1702341900070000,
    "highPrice": 17540,
    "highTime": 1702342491330000,
    "lowPrice": 17427,
    "lowTime": 1702355400574000,
    "closePrice": 17460,
    "closeTime": 1702359886936000,
    "avgPrice": 17478.89,
    "change": 44,
    "changePercent": 0.25,
    "amplitude": 0.65,
    "lastPrice": 17460,
    "lastSize": 1,
    "total": {"tradeVolume": 1626, "totalBidMatch": 0, "totalAskMatch": 0},
    "lastTrade": {"bid": 17459, "ask": 17460, "price": 17460, "size": 1, "time": 1702359886936000, "serial": 165753},
    "lastTrial": {"price": 45380, "size": 1, "time": 1781506205000000, "serial": 3},
    "serial": 165753,
    "lastUpdated": 1702359886936000,
}


def _make_sdk(responses: dict[str, Any]) -> MagicMock:
    """Build a mock SDK where quote(symbol=X) returns responses[X]."""
    sdk = MagicMock()

    def _quote(symbol: str) -> Any:
        if symbol not in responses:
            raise RuntimeError(f"unknown symbol {symbol}")
        return responses[symbol]

    sdk.marketdata.rest_client.futopt.intraday.quote.side_effect = _quote
    return sdk


def _quote(**overrides: Any) -> dict[str, Any]:
    return {**DOCUMENTED_QUOTE, **overrides}


def _translate(raw: Any, symbol: str = "TXFA4") -> dict[str, Any]:
    return FubonSnapshotFetcher._translate(symbol, raw)


# ------------------------------------------------------------------ #
# _coerce_price / _coerce_int tests
# ------------------------------------------------------------------ #


class TestCoercePrice:
    def test_float(self) -> None:
        assert _coerce_price(100.5) == 100.5

    def test_int(self) -> None:
        assert _coerce_price(200) == 200.0

    def test_str_numeric(self) -> None:
        assert _coerce_price("50.25") == 50.25

    def test_none_returns_zero(self) -> None:
        assert _coerce_price(None) == 0.0

    def test_unparseable_str_returns_zero(self) -> None:
        assert _coerce_price("N/A") == 0.0

    def test_zero(self) -> None:
        assert _coerce_price(0) == 0.0

    def test_negative(self) -> None:
        assert _coerce_price(-10.0) == -10.0


class TestCoerceInt:
    @pytest.mark.parametrize(("raw", "expected"), [(5, 5), (5.0, 5), ("7", 7), (0, 0), (-3, -3)])
    def test_numbers_are_coerced(self, raw: Any, expected: int) -> None:
        assert _coerce_int(raw) == expected

    @pytest.mark.parametrize("raw", [None, "N/A", [], {}, float("inf"), float("nan")])
    def test_missing_or_unparseable_returns_zero(self, raw: Any) -> None:
        assert _coerce_int(raw) == 0


# ------------------------------------------------------------------ #
# _translate: the documented response
# ------------------------------------------------------------------ #


class TestTranslateDocumentedQuote:
    def test_documented_example_maps_last_trade_totals_and_ohlc(self) -> None:
        snap = _translate(DOCUMENTED_QUOTE)

        assert snap["code"] == "TXFA4"
        assert snap["close"] == 17460.0  # lastTrade.price
        assert snap["volume"] == 1  # lastTrade.size
        assert snap["total_volume"] == 1626  # total.tradeVolume
        assert snap["open"] == 17514.0
        assert snap["high"] == 17540.0
        assert snap["low"] == 17427.0
        assert isinstance(snap["ts"], int)
        assert snap["ts"] > 0

    def test_prices_stay_raw_numbers_for_the_normalizer_to_scale(self) -> None:
        snap = _translate(DOCUMENTED_QUOTE)

        assert snap["close"] == 17460.0
        assert snap["close"] != 17460 * 10_000

    def test_trial_values_are_never_used_as_the_last_price(self) -> None:
        """lastPrice / lastSize include trial matching; lastTrade is the last real trade."""
        raw = _quote(lastPrice=45380, lastSize=9, isTrial=True, change=9999)

        snap = _translate(raw)

        assert snap["close"] == 17460.0
        assert snap["volume"] == 1

    def test_last_trial_block_is_ignored(self) -> None:
        raw = _quote(lastTrial={"price": 99999, "size": 77, "time": 1781506205000000, "serial": 3})

        snap = _translate(raw)

        assert snap["close"] == 17460.0
        assert snap["volume"] == 1

    def test_stale_last_trade_quotes_are_not_forwarded_as_a_book(self) -> None:
        """lastTrade.bid / ask are the quotes at the last trade, possibly hours old: no book levels."""
        snap = _translate(DOCUMENTED_QUOTE)

        assert snap["buy_price"] == 0.0
        assert snap["sell_price"] == 0.0
        assert snap["buy_volume"] == 0
        assert snap["sell_volume"] == 0

    def test_ts_is_the_fetch_time_not_the_response_last_updated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(snapshot_fetcher.timebase, "now_ns", lambda: 123_456_789)

        snap = _translate(DOCUMENTED_QUOTE)

        assert snap["ts"] == 123_456_789

    def test_before_the_first_trade_there_is_no_last_trade_block(self) -> None:
        raw = {k: v for k, v in DOCUMENTED_QUOTE.items() if k not in ("lastTrade", "lastPrice", "lastSize")}

        snap = _translate(raw)

        assert snap["close"] == 0.0
        assert snap["volume"] == 0
        assert snap["open"] == 17514.0

    @pytest.mark.parametrize("bad", [None, [], "x", 5])
    def test_nested_sections_that_are_not_objects_default_to_zero(self, bad: Any) -> None:
        snap = _translate(_quote(lastTrade=bad, total=bad))

        assert snap["close"] == 0.0
        assert snap["volume"] == 0
        assert snap["total_volume"] == 0

    def test_empty_response_defaults_to_zero(self) -> None:
        snap = _translate({})

        assert snap["code"] == "TXFA4"
        assert snap["close"] == 0.0
        assert snap["volume"] == 0
        assert snap["total_volume"] == 0
        assert snap["open"] == 0.0
        assert snap["high"] == 0.0
        assert snap["low"] == 0.0

    def test_none_fields_default_to_zero(self) -> None:
        raw = {
            "openPrice": None,
            "highPrice": None,
            "lowPrice": None,
            "lastTrade": {"price": None, "size": None},
            "total": {"tradeVolume": None},
        }

        snap = _translate(raw)

        assert snap["close"] == 0.0
        assert snap["volume"] == 0
        assert snap["total_volume"] == 0
        assert snap["open"] == 0.0

    def test_legacy_flat_keys_are_no_longer_read(self) -> None:
        """Nothing produces the old flat shape any more; only the documented fields count."""
        legacy = {
            "close": 100.5,
            "volume": 1234,
            "bid_price": 100.0,
            "ask_price": 101.0,
            "bid_volume": 50,
            "ask_volume": 30,
            "open": 99.0,
            "high": 102.0,
            "low": 98.0,
        }

        snap = _translate(legacy)

        assert snap["close"] == 0.0
        assert snap["volume"] == 0
        assert snap["buy_price"] == 0.0
        assert snap["sell_price"] == 0.0
        assert snap["open"] == 0.0
        assert snap["high"] == 0.0
        assert snap["low"] == 0.0

    @pytest.mark.parametrize("raw", [None, [], "TXFA4", SimpleNamespace(lastTrade={"price": 1})])
    def test_non_object_response_is_rejected(self, raw: Any) -> None:
        with pytest.raises(TypeError, match="must be an object"):
            _translate(raw)


# ------------------------------------------------------------------ #
# Consumer contract: the real normalizer
# ------------------------------------------------------------------ #


class TestNormalizerContract:
    @pytest.fixture
    def normalizer(self, tmp_path: Any) -> MarketDataNormalizer:
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text("symbols:\n  - code: 'TXFA4'\n    exchange: 'TAIFEX'\n    price_scale: 10000\n")
        return MarketDataNormalizer(str(cfg))

    def test_documented_example_becomes_an_empty_book_snapshot(self, normalizer: MarketDataNormalizer) -> None:
        event = normalizer.normalize_snapshot(_translate(DOCUMENTED_QUOTE))

        assert isinstance(event, BidAskEvent)
        assert event.is_snapshot is True
        assert event.symbol == "TXFA4"
        assert len(event.bids) == 0  # no stale last-trade quote seeded into the book
        assert len(event.asks) == 0


# ------------------------------------------------------------------ #
# FubonSnapshotFetcher tests
# ------------------------------------------------------------------ #


class TestFetchSnapshots:
    def test_valid_single_symbol(self) -> None:
        sdk = _make_sdk({"TXFA4": DOCUMENTED_QUOTE})
        fetcher = FubonSnapshotFetcher(sdk)
        result = fetcher.fetch_snapshots(["TXFA4"])

        assert len(result) == 1
        snap = result[0]
        assert snap["code"] == "TXFA4"
        assert snap["close"] == 17460.0  # raw number — normalizer handles scaling
        assert snap["volume"] == 1
        assert snap["total_volume"] == 1626
        assert snap["open"] == 17514.0
        assert snap["high"] == 17540.0
        assert snap["low"] == 17427.0
        assert isinstance(snap["ts"], int)
        assert snap["ts"] > 0

    def test_fetch_calls_futopt_rest_quote_with_symbol_keyword(self) -> None:
        """fubon-neo 2.5.0: futures/options quotes are rest_client.futopt.intraday.quote(symbol=...)."""
        sdk = _make_sdk({"TXFA4": DOCUMENTED_QUOTE})
        fetcher = FubonSnapshotFetcher(sdk)

        result = fetcher.fetch_snapshots(["TXFA4"])

        sdk.marketdata.rest_client.futopt.intraday.quote.assert_called_once_with(symbol="TXFA4")
        assert [snap["code"] for snap in result] == ["TXFA4"]

    def test_fetch_never_calls_stock_rest_quote(self) -> None:
        sdk = _make_sdk({"TXFA4": DOCUMENTED_QUOTE})
        fetcher = FubonSnapshotFetcher(sdk)

        fetcher.fetch_snapshots(["TXFA4"])

        assert sdk.marketdata.rest_client.stock.intraday.quote.call_count == 0

    def test_fetch_before_init_realtime_returns_empty(self) -> None:
        """sdk.marketdata only exists after init_realtime(); a missing attribute must not crash startup."""
        sdk = SimpleNamespace()  # no ``marketdata`` attribute
        fetcher = FubonSnapshotFetcher(sdk)

        assert fetcher.fetch_snapshots(["TXFA4"]) == []

    def test_multiple_symbols(self) -> None:
        sdk = _make_sdk(
            {
                "TXFA4": _quote(lastTrade={"price": 17460, "size": 1}),
                "TMFA4": _quote(lastTrade={"price": 17461, "size": 2}),
            }
        )
        fetcher = FubonSnapshotFetcher(sdk)
        result = fetcher.fetch_snapshots(["TXFA4", "TMFA4"])

        assert len(result) == 2
        assert result[0]["code"] == "TXFA4"
        assert result[0]["close"] == 17460.0
        assert result[1]["code"] == "TMFA4"
        assert result[1]["close"] == 17461.0
        assert result[1]["volume"] == 2

    def test_empty_symbols_returns_empty(self) -> None:
        sdk = _make_sdk({})
        fetcher = FubonSnapshotFetcher(sdk)
        assert fetcher.fetch_snapshots([]) == []

    def test_partial_failure_skips_failed(self) -> None:
        """Two symbols: first succeeds, second raises. Only first returned."""
        sdk = _make_sdk({"TXFA4": DOCUMENTED_QUOTE})
        # "9999" is not in responses dict → raises RuntimeError
        fetcher = FubonSnapshotFetcher(sdk)
        result = fetcher.fetch_snapshots(["TXFA4", "9999"])

        assert len(result) == 1
        assert result[0]["code"] == "TXFA4"

    def test_all_fail_returns_empty(self) -> None:
        sdk = _make_sdk({})
        fetcher = FubonSnapshotFetcher(sdk)
        result = fetcher.fetch_snapshots(["BAD1", "BAD2"])
        assert result == []

    def test_empty_response_defaults_to_zero(self) -> None:
        sdk = _make_sdk({"TXFA4": {}})
        fetcher = FubonSnapshotFetcher(sdk)
        result = fetcher.fetch_snapshots(["TXFA4"])

        assert len(result) == 1
        snap = result[0]
        assert snap["code"] == "TXFA4"
        assert snap["close"] == 0.0
        assert snap["volume"] == 0
        assert snap["buy_price"] == 0.0
        assert snap["sell_price"] == 0.0

    def test_object_style_response_is_skipped_not_guessed(self) -> None:
        """The REST client returns parsed JSON; an object with attributes is not a documented shape."""
        resp = SimpleNamespace(lastTrade={"price": 17460, "size": 1})
        sdk = MagicMock()
        sdk.marketdata.rest_client.futopt.intraday.quote.return_value = resp
        fetcher = FubonSnapshotFetcher(sdk)

        assert fetcher.fetch_snapshots(["TXFA4"]) == []
