"""Tests for Fubon market data provider."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import numpy as np
import pytest

from hft_platform.feed_adapter.fubon.market_data import (
    BOOK_DEPTH,
    PRICE_SCALE,
    FubonMarketDataProvider,
)
from hft_platform.feed_adapter.fubon.normalizer_fields import (
    FUBON_FIELD_MAP,
    NormalizerFieldMap,
)
from hft_platform.feed_adapter.protocols import MarketDataProvider


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture()
def mock_sdk() -> MagicMock:
    sdk = MagicMock()
    sdk.marketdata.websocket_client.stock = MagicMock()
    sdk.marketdata.rest_client.stock.intraday.quote = MagicMock()
    return sdk


@pytest.fixture()
def provider(mock_sdk: MagicMock) -> FubonMarketDataProvider:
    return FubonMarketDataProvider(
        sdk=mock_sdk,
        account=MagicMock(),
        symbols=["2881", "2330"],
    )


# ── Protocol conformance ────────────────────────────────────────


def test_protocol_conformance(provider: FubonMarketDataProvider) -> None:
    """FubonMarketDataProvider satisfies MarketDataProvider protocol."""
    assert isinstance(provider, MarketDataProvider)


# ── subscribe_basket ─────────────────────────────────────────────


def test_subscribe_basket_connects_and_subscribes(
    provider: FubonMarketDataProvider,
    mock_sdk: MagicMock,
) -> None:
    cb = MagicMock()
    provider.subscribe_basket(cb)

    mock_sdk.init_realtime.assert_called_once()

    ws = mock_sdk.marketdata.websocket_client.stock
    ws.connect.assert_called_once()

    # 2 symbols x 2 channels = 4 subscribe calls
    assert ws.subscribe.call_count == 4
    ws.subscribe.assert_any_call({"channel": "trades", "symbol": "2881"})
    ws.subscribe.assert_any_call({"channel": "books", "symbol": "2881"})
    ws.subscribe.assert_any_call({"channel": "trades", "symbol": "2330"})
    ws.subscribe.assert_any_call({"channel": "books", "symbol": "2330"})


def test_subscribe_basket_registers_event_handlers(
    provider: FubonMarketDataProvider,
    mock_sdk: MagicMock,
) -> None:
    provider.subscribe_basket(MagicMock())

    ws = mock_sdk.marketdata.websocket_client.stock
    ws.on.assert_any_call("message", provider._on_message)
    ws.on.assert_any_call("connect", provider._on_connect)
    ws.on.assert_any_call("disconnect", provider._on_disconnect)
    ws.on.assert_any_call("error", provider._on_error)


def test_subscribe_basket_raises_on_init_failure(
    provider: FubonMarketDataProvider,
    mock_sdk: MagicMock,
) -> None:
    mock_sdk.init_realtime.side_effect = RuntimeError("init failed")
    with pytest.raises(RuntimeError, match="init failed"):
        provider.subscribe_basket(MagicMock())


# ── _handle_trade ────────────────────────────────────────────────


def test_handle_trade_price_scaling(
    provider: FubonMarketDataProvider,
) -> None:
    """Fubon trade price '66.5' -> scaled int 665000."""
    cb = MagicMock()
    provider._callback = cb

    trade_msg = json.dumps({
        "event": "",
        "channel": "trades",
        "data": {
            "symbol": "2881",
            "price": "66.5",
            "size": 10,
            "volume": 1000,
            "bid": "66.4",
            "ask": "66.6",
            "time": 1234567890,
            "isTrial": False,
        },
    })

    provider._on_message(trade_msg)

    cb.assert_called_once()
    tick = cb.call_args[0][0]
    assert tick["code"] == "2881"
    assert tick["close"] == 665000  # 66.5 * 10000
    assert tick["volume"] == 10
    assert tick["total_volume"] == 1000
    assert tick["bid_price"] == 664000  # 66.4 * 10000
    assert tick["ask_price"] == 666000  # 66.6 * 10000
    assert tick["simtrade"] is False


def test_handle_trade_string_price_precision(
    provider: FubonMarketDataProvider,
) -> None:
    """Verify Decimal conversion avoids float rounding errors."""
    cb = MagicMock()
    provider._callback = cb

    # 100.1 + 0.2 = 100.3 exactly via Decimal, not 100.30000000000001
    trade_msg = json.dumps({
        "channel": "trades",
        "data": {
            "symbol": "2330",
            "price": "100.3",
            "size": 1,
            "volume": 100,
            "time": 0,
        },
    })

    provider._on_message(trade_msg)

    tick = cb.call_args[0][0]
    assert tick["close"] == 1003000  # Exact: 100.3 * 10000


def test_handle_trade_skips_missing_price(
    provider: FubonMarketDataProvider,
) -> None:
    cb = MagicMock()
    provider._callback = cb

    trade_msg = json.dumps({
        "channel": "trades",
        "data": {"symbol": "2881", "size": 10},
    })

    provider._on_message(trade_msg)
    cb.assert_not_called()


def test_handle_trade_no_bid_ask(
    provider: FubonMarketDataProvider,
) -> None:
    """Trade message without bid/ask should default to 0."""
    cb = MagicMock()
    provider._callback = cb

    trade_msg = json.dumps({
        "channel": "trades",
        "data": {
            "symbol": "2881",
            "price": "50.0",
            "size": 5,
            "time": 0,
        },
    })

    provider._on_message(trade_msg)
    tick = cb.call_args[0][0]
    assert tick["bid_price"] == 0
    assert tick["ask_price"] == 0


# ── _handle_book ─────────────────────────────────────────────────


def test_handle_book_flattens_bids_asks(
    provider: FubonMarketDataProvider,
) -> None:
    """Book message bids[{price,size}] -> parallel arrays."""
    cb = MagicMock()
    provider._callback = cb

    book_msg = json.dumps({
        "channel": "books",
        "data": {
            "symbol": "2881",
            "time": 9999,
            "bids": [
                {"price": "66.5", "size": 100},
                {"price": "66.4", "size": 200},
                {"price": "66.3", "size": 300},
            ],
            "asks": [
                {"price": "66.6", "size": 50},
                {"price": "66.7", "size": 150},
            ],
        },
    })

    provider._on_message(book_msg)

    cb.assert_called_once()
    bidask = cb.call_args[0][0]

    assert bidask["code"] == "2881"
    assert bidask["ts"] == 9999

    # Bid prices
    np.testing.assert_array_equal(
        bidask["bid_price"][:3],
        [665000, 664000, 663000],
    )
    np.testing.assert_array_equal(
        bidask["bid_volume"][:3],
        [100, 200, 300],
    )
    # Remaining slots zero-filled
    np.testing.assert_array_equal(bidask["bid_price"][3:], [0, 0])
    np.testing.assert_array_equal(bidask["bid_volume"][3:], [0, 0])

    # Ask prices
    np.testing.assert_array_equal(
        bidask["ask_price"][:2],
        [666000, 667000],
    )
    np.testing.assert_array_equal(
        bidask["ask_volume"][:2],
        [50, 150],
    )
    np.testing.assert_array_equal(bidask["ask_price"][2:], [0, 0, 0])


def test_handle_book_reuses_preallocated_buffers(
    provider: FubonMarketDataProvider,
) -> None:
    """Verify buffers are reused (same underlying object) across calls."""
    cb = MagicMock()
    provider._callback = cb

    bid_buf_id = id(provider._bid_buf)
    ask_buf_id = id(provider._ask_buf)

    book_msg = json.dumps({
        "channel": "books",
        "data": {
            "symbol": "2881",
            "time": 0,
            "bids": [{"price": "10", "size": 1}],
            "asks": [{"price": "11", "size": 1}],
        },
    })

    provider._on_message(book_msg)
    provider._on_message(book_msg)

    # Internal buffers should be the same object (not reallocated)
    assert id(provider._bid_buf) == bid_buf_id
    assert id(provider._ask_buf) == ask_buf_id

    # But output arrays should be copies (not the buffer itself)
    first_call = cb.call_args_list[0][0][0]
    second_call = cb.call_args_list[1][0][0]
    assert first_call["bid_price"] is not provider._bid_buf[:, 0]


def test_handle_book_more_than_depth_levels(
    provider: FubonMarketDataProvider,
) -> None:
    """Only top BOOK_DEPTH levels are used; extras are ignored."""
    cb = MagicMock()
    provider._callback = cb

    bids = [{"price": str(100 - i), "size": i + 1} for i in range(10)]
    book_msg = json.dumps({
        "channel": "books",
        "data": {
            "symbol": "2881",
            "time": 0,
            "bids": bids,
            "asks": [],
        },
    })

    provider._on_message(book_msg)

    bidask = cb.call_args[0][0]
    assert len(bidask["bid_price"]) == BOOK_DEPTH


# ── Control message filtering ───────────────────────────────────


@pytest.mark.parametrize(
    "event",
    ["authenticated", "subscribed", "unsubscribed", "heartbeat", "pong"],
)
def test_control_messages_ignored(
    provider: FubonMarketDataProvider,
    event: str,
) -> None:
    cb = MagicMock()
    provider._callback = cb

    msg = json.dumps({"event": event, "data": {}})
    provider._on_message(msg)

    cb.assert_not_called()


def test_error_event_logged_not_forwarded(
    provider: FubonMarketDataProvider,
) -> None:
    cb = MagicMock()
    provider._callback = cb

    msg = json.dumps({"event": "error", "data": {"message": "bad"}})
    provider._on_message(msg)

    cb.assert_not_called()


# ── fetch_snapshots ──────────────────────────────────────────────


def test_fetch_snapshots_returns_list(
    provider: FubonMarketDataProvider,
    mock_sdk: MagicMock,
) -> None:
    result = MagicMock()
    result.data = {"symbol": "2881", "price": 66.5}
    mock_sdk.marketdata.rest_client.stock.intraday.quote.return_value = result

    snapshots = provider.fetch_snapshots()
    assert len(snapshots) == 2  # 2 symbols


def test_fetch_snapshots_handles_failure(
    provider: FubonMarketDataProvider,
    mock_sdk: MagicMock,
) -> None:
    mock_sdk.marketdata.rest_client.stock.intraday.quote.side_effect = (
        RuntimeError("network")
    )
    snapshots = provider.fetch_snapshots()
    assert snapshots == []


# ── resubscribe ──────────────────────────────────────────────────


def test_resubscribe_success(
    provider: FubonMarketDataProvider,
    mock_sdk: MagicMock,
) -> None:
    provider.subscribe_basket(MagicMock())
    ws = mock_sdk.marketdata.websocket_client.stock
    ws.subscribe.reset_mock()

    result = provider.resubscribe()
    assert result is True
    assert ws.subscribe.call_count == 4


def test_resubscribe_false_when_disconnected(
    provider: FubonMarketDataProvider,
) -> None:
    # Never connected
    assert provider.resubscribe() is False


def test_resubscribe_false_on_exception(
    provider: FubonMarketDataProvider,
    mock_sdk: MagicMock,
) -> None:
    provider.subscribe_basket(MagicMock())
    ws = mock_sdk.marketdata.websocket_client.stock
    ws.subscribe.side_effect = RuntimeError("ws error")

    result = provider.resubscribe()
    assert result is False


# ── validate_symbols ─────────────────────────────────────────────


def test_validate_symbols_returns_empty(
    provider: FubonMarketDataProvider,
) -> None:
    assert provider.validate_symbols() == []


# ── reload_symbols ───────────────────────────────────────────────


def test_reload_symbols_noop(provider: FubonMarketDataProvider) -> None:
    # Should not raise
    provider.reload_symbols()


# ── Connection callbacks ─────────────────────────────────────────


def test_on_connect_sets_connected(
    provider: FubonMarketDataProvider,
) -> None:
    provider._on_connect()
    assert provider._connected is True


def test_on_disconnect_clears_connected(
    provider: FubonMarketDataProvider,
) -> None:
    provider._connected = True
    provider._on_disconnect()
    assert provider._connected is False


def test_on_error_does_not_raise(
    provider: FubonMarketDataProvider,
) -> None:
    # Should not raise
    provider._on_error(RuntimeError("test"))


# ── Normalizer field map ────────────────────────────────────────


def test_fubon_field_map_is_frozen() -> None:
    with pytest.raises(AttributeError):
        FUBON_FIELD_MAP.symbol_key = "changed"  # type: ignore[misc]


def test_fubon_field_map_values() -> None:
    assert FUBON_FIELD_MAP.symbol_key == "symbol"
    assert FUBON_FIELD_MAP.price_key == "price"
    assert FUBON_FIELD_MAP.volume_key == "size"
    assert FUBON_FIELD_MAP.ts_key == "time"
    assert FUBON_FIELD_MAP.simtrade_key == "isTrial"


def test_default_field_map_matches_shioaji_convention() -> None:
    """Default NormalizerFieldMap uses Shioaji-compatible keys."""
    default = NormalizerFieldMap()
    assert default.symbol_key == "code"
    assert default.price_key == "close"


# ── Config ───────────────────────────────────────────────────────


def test_fubon_config_frozen() -> None:
    from hft_platform.feed_adapter.fubon._config import FubonClientConfig

    cfg = FubonClientConfig()
    with pytest.raises(AttributeError):
        cfg.simulation = False  # type: ignore[misc]


def test_fubon_config_defaults() -> None:
    from hft_platform.feed_adapter.fubon._config import FubonClientConfig

    cfg = FubonClientConfig()
    assert cfg.simulation is True
    assert cfg.reconnect_max_retries == 5
    assert cfg.reconnect_backoff_s == 2.0


# ── Edge: malformed messages ─────────────────────────────────────


def test_malformed_json_does_not_raise(
    provider: FubonMarketDataProvider,
) -> None:
    provider._callback = MagicMock()
    provider._on_message("not valid json {{{")
    provider._callback.assert_not_called()


def test_dict_message_passthrough(
    provider: FubonMarketDataProvider,
) -> None:
    """_on_message should handle dict input (already parsed)."""
    cb = MagicMock()
    provider._callback = cb

    provider._on_message({  # type: ignore[arg-type]
        "channel": "trades",
        "data": {
            "symbol": "2881",
            "price": "50",
            "size": 1,
            "time": 0,
        },
    })

    cb.assert_called_once()
    tick = cb.call_args[0][0]
    assert tick["close"] == 500000


# ── Pre-allocated buffer shape ───────────────────────────────────


def test_buffer_shape(provider: FubonMarketDataProvider) -> None:
    assert provider._bid_buf.shape == (BOOK_DEPTH, 2)
    assert provider._ask_buf.shape == (BOOK_DEPTH, 2)
    assert provider._bid_buf.dtype == np.int64
    assert provider._ask_buf.dtype == np.int64
