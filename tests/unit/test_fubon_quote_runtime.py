"""Unit tests for FubonQuoteRuntime.

Tests cover:
- Trade callback translation (Fubon format → canonical tick dict)
- Book callback translation (Fubon format → canonical bidask dict)
- Subscribe / unsubscribe lifecycle
- Watchdog start / stop
- Pre-allocated buffer reuse (same dict object on every callback)
- Price scaling (float → int x10000)
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sdk() -> MagicMock:
    """Return a mock Fubon SDK with the expected WebSocket API surface."""
    sdk = MagicMock()
    sdk.marketdata.websocket_client.stock.subscribe = MagicMock()
    sdk.marketdata.websocket_client.stock.unsubscribe = MagicMock()
    return sdk


def _make_runtime(sdk: MagicMock | None = None):
    from hft_platform.feed_adapter.fubon.quote_runtime import FubonQuoteRuntime

    return FubonQuoteRuntime(sdk or _make_sdk())


# ---------------------------------------------------------------------------
# Trade callback translation
# ---------------------------------------------------------------------------


class TestTradeCallback:
    def test_trade_translates_to_canonical_tick(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        fubon_trade = {
            "symbol": "2330",
            "close": 595.0,
            "volume": 3,
            "datetime": 1_700_000_000_000_000_000,  # already ns
        }
        rt._on_fubon_trade(fubon_trade)

        assert len(received) == 1
        tick = received[0]
        assert tick["code"] == "2330"
        assert tick["close"] == 5_950_000  # 595.0 * 10000
        assert tick["volume"] == 3
        assert tick["ts"] == 1_700_000_000_000_000_000

    def test_trade_uses_price_field_fallback(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        fubon_trade = {
            "symbol": "2317",
            "price": 100.5,
            "volume": 1,
            "datetime": 0,
        }
        rt._on_fubon_trade(fubon_trade)

        assert len(received) == 1
        assert received[0]["close"] == 1_005_000  # 100.5 * 10000

    def test_trade_price_scaling_precision(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        fubon_trade = {"symbol": "2330", "close": 0.01, "volume": 1, "datetime": 0}
        rt._on_fubon_trade(fubon_trade)
        assert received[0]["close"] == 100  # 0.01 * 10000

    def test_trade_no_callback_noop(self) -> None:
        """No crash when on_tick is not registered."""
        rt = _make_runtime()
        rt._on_fubon_trade({"symbol": "2330", "close": 100.0, "volume": 1, "datetime": 0})
        # Should silently return without error

    def test_trade_callback_error_logged(self) -> None:
        """Malformed data should not crash; error is logged."""
        rt = _make_runtime()
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=MagicMock())
        # 'close' is a non-numeric string — will fail float()
        rt._on_fubon_trade({"symbol": "2330", "close": "bad", "volume": 1, "datetime": 0})
        # No exception propagated


# ---------------------------------------------------------------------------
# Book callback translation
# ---------------------------------------------------------------------------


class TestBookCallback:
    def test_book_translates_to_canonical_bidask(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []

        def capture(d: dict) -> None:
            # Deep-copy lists so mutations don't affect our snapshot
            received.append({
                "code": d["code"],
                "bid_price": list(d["bid_price"]),
                "bid_volume": list(d["bid_volume"]),
                "ask_price": list(d["ask_price"]),
                "ask_volume": list(d["ask_volume"]),
                "ts": d["ts"],
            })

        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=capture)

        fubon_book = {
            "symbol": "2330",
            "bid_prices": [595.0, 594.0, 593.0, 592.0, 591.0],
            "bid_sizes": [10, 20, 30, 40, 50],
            "ask_prices": [596.0, 597.0, 598.0, 599.0, 600.0],
            "ask_sizes": [5, 15, 25, 35, 45],
            "datetime": 1_700_000_000_000_000_000,
        }
        rt._on_fubon_book(fubon_book)

        assert len(received) == 1
        book = received[0]
        assert book["code"] == "2330"
        assert book["bid_price"] == [5_950_000, 5_940_000, 5_930_000, 5_920_000, 5_910_000]
        assert book["bid_volume"] == [10, 20, 30, 40, 50]
        assert book["ask_price"] == [5_960_000, 5_970_000, 5_980_000, 5_990_000, 6_000_000]
        assert book["ask_volume"] == [5, 15, 25, 35, 45]
        assert book["ts"] == 1_700_000_000_000_000_000

    def test_book_fewer_than_5_levels(self) -> None:
        """If Fubon sends fewer than 5 levels, remaining slots are zero-filled."""
        rt = _make_runtime()
        received: list[dict[str, Any]] = []

        def capture(d: dict) -> None:
            received.append({
                "bid_price": list(d["bid_price"]),
                "bid_volume": list(d["bid_volume"]),
                "ask_price": list(d["ask_price"]),
                "ask_volume": list(d["ask_volume"]),
            })

        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=capture)

        fubon_book = {
            "symbol": "2330",
            "bid_prices": [595.0, 594.0],
            "bid_sizes": [10, 20],
            "ask_prices": [596.0],
            "ask_sizes": [5],
            "datetime": 0,
        }
        rt._on_fubon_book(fubon_book)

        assert len(received) == 1
        book = received[0]
        assert book["bid_price"][2:] == [0, 0, 0]
        assert book["bid_volume"][2:] == [0, 0, 0]
        assert book["ask_price"][1:] == [0, 0, 0, 0]
        assert book["ask_volume"][1:] == [0, 0, 0, 0]

    def test_book_no_callback_noop(self) -> None:
        rt = _make_runtime()
        rt._on_fubon_book({"symbol": "2330", "bid_prices": [], "bid_sizes": [], "ask_prices": [], "ask_sizes": [], "datetime": 0})


# ---------------------------------------------------------------------------
# Buffer reuse
# ---------------------------------------------------------------------------


class TestBufferReuse:
    def test_tick_buffer_same_object(self) -> None:
        """The tick callback must receive the *same* dict object each time."""
        rt = _make_runtime()
        ids: list[int] = []
        rt.register_quote_callbacks(on_tick=lambda d: ids.append(id(d)), on_bidask=MagicMock())

        rt._on_fubon_trade({"symbol": "2330", "close": 100.0, "volume": 1, "datetime": 0})
        rt._on_fubon_trade({"symbol": "2317", "close": 200.0, "volume": 2, "datetime": 0})

        assert len(ids) == 2
        assert ids[0] == ids[1], "tick buffer must be the same dict object (Allocator Law)"

    def test_bidask_buffer_same_object(self) -> None:
        """The bidask callback must receive the *same* dict object each time."""
        rt = _make_runtime()
        ids: list[int] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: ids.append(id(d)))

        book = {
            "symbol": "2330",
            "bid_prices": [100.0] * 5,
            "bid_sizes": [1] * 5,
            "ask_prices": [101.0] * 5,
            "ask_sizes": [1] * 5,
            "datetime": 0,
        }
        rt._on_fubon_book(book)
        rt._on_fubon_book(book)

        assert len(ids) == 2
        assert ids[0] == ids[1], "bidask buffer must be the same dict object (Allocator Law)"


# ---------------------------------------------------------------------------
# Subscribe / unsubscribe
# ---------------------------------------------------------------------------


class TestSubscription:
    def test_subscribe_calls_sdk(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["2330", "2317"])

        assert sdk.init_realtime.call_count == 1
        ws = sdk.marketdata.websocket_client.stock
        # 2 symbols x 2 channels = 4 subscribe calls
        assert ws.subscribe.call_count == 4

    def test_subscribe_idempotent(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["2330"])
        rt.subscribe(["2330"])  # duplicate

        ws = sdk.marketdata.websocket_client.stock
        assert ws.subscribe.call_count == 2  # only first subscription

    def test_init_realtime_called_once(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["2330"])
        rt.subscribe(["2317"])

        assert sdk.init_realtime.call_count == 1

    def test_unsubscribe_calls_sdk(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["2330"])
        rt.unsubscribe(["2330"])

        ws = sdk.marketdata.websocket_client.stock
        assert ws.unsubscribe.call_count == 2  # trades + books

    def test_unsubscribe_unknown_symbol_noop(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)
        rt.unsubscribe(["9999"])

        ws = sdk.marketdata.websocket_client.stock
        assert ws.unsubscribe.call_count == 0


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


class TestWatchdog:
    def test_watchdog_starts_and_stops(self) -> None:
        rt = _make_runtime()
        rt.start_quote_watchdog(timeout_s=0.05)

        assert rt._watchdog_thread is not None
        assert rt._watchdog_thread.is_alive()

        rt.stop()
        # Give daemon thread time to exit
        time.sleep(0.15)
        assert not rt._watchdog_thread.is_alive() if rt._watchdog_thread else True

    def test_watchdog_no_duplicate_start(self) -> None:
        rt = _make_runtime()
        rt.start_quote_watchdog(timeout_s=60.0)
        first_thread = rt._watchdog_thread
        rt.start_quote_watchdog(timeout_s=60.0)

        assert rt._watchdog_thread is first_thread
        rt.stop()


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_unsubscribes_all(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)
        rt.subscribe(["2330", "2317"])
        rt.stop()

        ws = sdk.marketdata.websocket_client.stock
        # 2 symbols x 2 channels = 4 unsubscribe calls
        assert ws.unsubscribe.call_count == 4
        assert len(rt._subscribed) == 0

    def test_stop_idempotent(self) -> None:
        rt = _make_runtime()
        rt.stop()
        rt.stop()  # second call must not crash


# ---------------------------------------------------------------------------
# Price scaling edge cases
# ---------------------------------------------------------------------------


class TestPriceScaling:
    def test_integer_price_scaled(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        rt._on_fubon_trade({"symbol": "2330", "close": 100, "volume": 1, "datetime": 0})
        assert received[0]["close"] == 1_000_000

    def test_small_float_price_scaled(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        rt._on_fubon_trade({"symbol": "2330", "close": 0.0001, "volume": 1, "datetime": 0})
        assert received[0]["close"] == 1  # 0.0001 * 10000

    def test_book_prices_scaled_correctly(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []

        def capture(d: dict) -> None:
            received.append({"bid_price": list(d["bid_price"]), "ask_price": list(d["ask_price"])})

        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=capture)

        rt._on_fubon_book({
            "symbol": "2330",
            "bid_prices": [100.5, 100.4, 100.3, 100.2, 100.1],
            "bid_sizes": [1, 1, 1, 1, 1],
            "ask_prices": [100.6, 100.7, 100.8, 100.9, 101.0],
            "ask_sizes": [1, 1, 1, 1, 1],
            "datetime": 0,
        })

        assert received[0]["bid_price"][0] == 1_005_000  # 100.5 * 10000
        assert received[0]["ask_price"][4] == 1_010_000  # 101.0 * 10000


# ---------------------------------------------------------------------------
# Object-style data (attribute access)
# ---------------------------------------------------------------------------


class TestObjectData:
    def test_trade_from_object(self) -> None:
        """Fubon SDK may return typed objects instead of dicts."""
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        obj = MagicMock()
        obj.symbol = "2330"
        obj.close = 595.0
        obj.volume = 3
        obj.datetime = 1_700_000_000_000_000_000
        # Ensure dict-style access fails so _get falls through to getattr
        obj.__contains__ = MagicMock(return_value=False)
        del obj.__getitem__

        rt._on_fubon_trade(obj)

        assert len(received) == 1
        assert received[0]["code"] == "2330"
        assert received[0]["close"] == 5_950_000
