"""Unit tests for FubonQuoteRuntime.

Tests cover:
- Trade callback translation (Fubon format → canonical tick dict)
- Book callback translation (Fubon format → canonical bidask dict)
- Subscribe / unsubscribe lifecycle
- Watchdog start / stop
- Snapshot isolation (each callback receives a distinct dict; workspace buffer
  reuse must not corrupt previously received messages across the async boundary)
- Raw price passthrough (prices must NOT be pre-scaled; normalizer handles scaling)
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
        assert tick["close"] == 595.0  # raw float — normalizer handles scaling
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
        assert received[0]["close"] == 100.5  # raw float — normalizer handles scaling

    def test_trade_price_scaling_precision(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        fubon_trade = {"symbol": "2330", "close": 0.01, "volume": 1, "datetime": 0}
        rt._on_fubon_trade(fubon_trade)
        assert received[0]["close"] == 0.01  # raw float — normalizer handles scaling

    def test_trade_no_callback_noop(self) -> None:
        """No crash when on_tick is not registered."""
        rt = _make_runtime()
        result = rt._on_fubon_trade({"symbol": "2330", "close": 100.0, "volume": 1, "datetime": 0})
        # Should silently return without error
        assert result is None

    def test_trade_callback_error_logged(self) -> None:
        """Malformed data should not crash; error is logged."""
        rt = _make_runtime()
        cb = MagicMock()
        rt.register_quote_callbacks(on_tick=cb, on_bidask=MagicMock())
        # 'close' is a non-numeric string — will fail float()
        result = rt._on_fubon_trade({"symbol": "2330", "close": "bad", "volume": 1, "datetime": 0})
        # No exception propagated; callback should not have been called with valid data
        assert result is None


# ---------------------------------------------------------------------------
# Book callback translation
# ---------------------------------------------------------------------------


class TestBookCallback:
    def test_book_translates_to_canonical_bidask(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []

        def capture(d: dict) -> None:
            # Deep-copy lists so mutations don't affect our snapshot
            received.append(
                {
                    "code": d["code"],
                    "bid_price": list(d["bid_price"]),
                    "bid_volume": list(d["bid_volume"]),
                    "ask_price": list(d["ask_price"]),
                    "ask_volume": list(d["ask_volume"]),
                    "ts": d["ts"],
                }
            )

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
        assert book["bid_price"] == [595.0, 594.0, 593.0, 592.0, 591.0]  # raw floats
        assert book["bid_volume"] == [10, 20, 30, 40, 50]
        assert book["ask_price"] == [596.0, 597.0, 598.0, 599.0, 600.0]  # raw floats
        assert book["ask_volume"] == [5, 15, 25, 35, 45]
        assert book["ts"] == 1_700_000_000_000_000_000

    def test_book_fewer_than_5_levels(self) -> None:
        """If Fubon sends fewer than 5 levels, remaining slots are zero-filled."""
        rt = _make_runtime()
        received: list[dict[str, Any]] = []

        def capture(d: dict) -> None:
            received.append(
                {
                    "bid_price": list(d["bid_price"]),
                    "bid_volume": list(d["bid_volume"]),
                    "ask_price": list(d["ask_price"]),
                    "ask_volume": list(d["ask_volume"]),
                }
            )

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
        result = rt._on_fubon_book(
            {"symbol": "2330", "bid_prices": [], "bid_sizes": [], "ask_prices": [], "ask_sizes": [], "datetime": 0}
        )
        assert result is None


# ---------------------------------------------------------------------------
# Buffer reuse
# ---------------------------------------------------------------------------


class TestBufferReuse:
    def test_tick_callback_receives_distinct_dicts(self) -> None:
        """Each tick callback invocation must receive a *distinct* dict object.

        The pre-allocated workspace buffer is reused for translation, but a
        fresh snapshot is passed to the callback so that the async consumer
        cannot observe a later callback's overwrite.

        We store the dict objects themselves (not just their ids) to prevent
        premature GC from causing id() reuse between callbacks.
        """
        rt = _make_runtime()
        received: list[dict] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(d), on_bidask=MagicMock())

        rt._on_fubon_trade({"symbol": "2330", "close": 100.0, "volume": 1, "datetime": 0})
        rt._on_fubon_trade({"symbol": "2317", "close": 200.0, "volume": 2, "datetime": 0})

        assert len(received) == 2
        assert received[0] is not received[1], "each tick callback must receive a fresh snapshot dict"

    def test_bidask_callback_receives_distinct_dicts(self) -> None:
        """Each bidask callback invocation must receive a *distinct* dict object.

        We store the dict objects themselves (not just their ids) to prevent
        premature GC from causing id() reuse between callbacks.
        """
        rt = _make_runtime()
        received: list[dict] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(d))

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

        assert len(received) == 2
        assert received[0] is not received[1], "each bidask callback must receive a fresh snapshot dict"

    def test_tick_snapshot_isolation(self) -> None:
        """Data from the first tick must not be overwritten when a second tick arrives.

        Simulates the async boundary: the callback stores a reference to the
        received dict (no copy), then fires a second trade. The first stored
        dict must still contain the first trade's values.
        """
        rt = _make_runtime()
        stored: list[dict] = []
        # Store the reference as-is — no copy — to simulate enqueue behaviour.
        rt.register_quote_callbacks(on_tick=lambda d: stored.append(d), on_bidask=MagicMock())

        rt._on_fubon_trade({"symbol": "2330", "close": 100.0, "volume": 1, "datetime": 0})
        rt._on_fubon_trade({"symbol": "2317", "close": 200.0, "volume": 2, "datetime": 0})

        assert len(stored) == 2
        # First snapshot must still reflect the first trade.
        assert stored[0]["code"] == "2330"
        assert stored[0]["close"] == 100.0  # raw float
        assert stored[0]["volume"] == 1
        # Second snapshot must reflect the second trade.
        assert stored[1]["code"] == "2317"
        assert stored[1]["close"] == 200.0  # raw float
        assert stored[1]["volume"] == 2

    def test_bidask_snapshot_isolation(self) -> None:
        """Nested bid/ask lists from the first book must not be overwritten by the second.

        Simulates the async boundary: the callback stores the dict reference
        without copying the inner lists. The first stored dict's lists must
        still contain the first book's prices after a second callback fires.
        """
        rt = _make_runtime()
        stored: list[dict] = []
        # Store the reference as-is — no copy of inner lists.
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: stored.append(d))

        book1 = {
            "symbol": "2330",
            "bid_prices": [595.0] * 5,
            "bid_sizes": [10] * 5,
            "ask_prices": [596.0] * 5,
            "ask_sizes": [5] * 5,
            "datetime": 0,
        }
        book2 = {
            "symbol": "2317",
            "bid_prices": [100.0] * 5,
            "bid_sizes": [1] * 5,
            "ask_prices": [101.0] * 5,
            "ask_sizes": [1] * 5,
            "datetime": 0,
        }
        rt._on_fubon_book(book1)
        rt._on_fubon_book(book2)

        assert len(stored) == 2
        # First snapshot's bid_price list must still contain book1 prices.
        assert stored[0]["bid_price"][0] == 595.0  # raw float
        assert stored[0]["ask_price"][0] == 596.0  # raw float
        # Second snapshot must contain book2 prices.
        assert stored[1]["bid_price"][0] == 100.0  # raw float
        assert stored[1]["ask_price"][0] == 101.0  # raw float


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
        assert len(rt._subscribed) == 0

    def test_late_callbacks_dropped_after_stop(self) -> None:
        """P1 (2026-04-24): Fubon SDK retains callback references after stop();
        late-delivered trade/book events must be discarded to prevent double
        publishing during resubscribe (FubonClient.resubscribe replaces the
        runtime without unregistering)."""
        rt = _make_runtime()
        tick_received: list[dict[str, Any]] = []
        book_received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(
            on_tick=lambda d: tick_received.append(dict(d)),
            on_bidask=lambda d: book_received.append(dict(d)),
        )
        # Before stop — deliveries succeed.
        rt._on_fubon_trade({"symbol": "2330", "close": 100.0, "volume": 1, "datetime": 0})
        rt._on_fubon_book(
            {
                "symbol": "2330",
                "bid_prices": [99.0],
                "bid_sizes": [10],
                "ask_prices": [101.0],
                "ask_sizes": [10],
                "datetime": 0,
            }
        )
        assert len(tick_received) == 1
        assert len(book_received) == 1

        rt.stop()

        # Simulate the Fubon SDK delivering a buffered callback post-stop.
        rt._on_fubon_trade({"symbol": "2330", "close": 200.0, "volume": 5, "datetime": 0})
        rt._on_fubon_book(
            {
                "symbol": "2330",
                "bid_prices": [199.0],
                "bid_sizes": [50],
                "ask_prices": [201.0],
                "ask_sizes": [50],
                "datetime": 0,
            }
        )
        # No additional deliveries — the old runtime refuses to forward.
        assert len(tick_received) == 1, "late trade callback leaked through stopped runtime"
        assert len(book_received) == 1, "late book callback leaked through stopped runtime"


# ---------------------------------------------------------------------------
# Price scaling edge cases
# ---------------------------------------------------------------------------


class TestPricePassthrough:
    def test_integer_price_coerced_to_float(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        rt._on_fubon_trade({"symbol": "2330", "close": 100, "volume": 1, "datetime": 0})
        assert received[0]["close"] == 100.0  # coerced to float, not scaled

    def test_small_float_price_passthrough(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        rt._on_fubon_trade({"symbol": "2330", "close": 0.0001, "volume": 1, "datetime": 0})
        assert received[0]["close"] == 0.0001  # raw float passthrough

    def test_book_prices_passthrough(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []

        def capture(d: dict) -> None:
            received.append({"bid_price": list(d["bid_price"]), "ask_price": list(d["ask_price"])})

        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=capture)

        rt._on_fubon_book(
            {
                "symbol": "2330",
                "bid_prices": [100.5, 100.4, 100.3, 100.2, 100.1],
                "bid_sizes": [1, 1, 1, 1, 1],
                "ask_prices": [100.6, 100.7, 100.8, 100.9, 101.0],
                "ask_sizes": [1, 1, 1, 1, 1],
                "datetime": 0,
            }
        )

        assert received[0]["bid_price"][0] == 100.5  # raw float passthrough
        assert received[0]["ask_price"][4] == 101.0  # raw float passthrough


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
        assert received[0]["close"] == 595.0  # raw float passthrough
