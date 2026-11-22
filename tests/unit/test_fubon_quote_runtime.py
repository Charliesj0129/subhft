"""Unit tests for FubonQuoteRuntime.

Tests cover:
- Trade translation (documented futopt trades payload -> canonical tick dict)
- Book translation (documented futopt books payload, 0-5 levels -> canonical bidask dict)
- Frame time: epoch microseconds -> nanoseconds, out-of-range times rejected
- Malformed payloads are dropped with a reason, never forwarded
- Subscribe / unsubscribe lifecycle
- Restart after stop() (generation counter): frames of the old connection stay dropped
- Watchdog start / stop
- Snapshot isolation (each callback receives a distinct dict; workspace buffer
  reuse must not corrupt previously received messages across the async boundary)
- Raw price passthrough (prices must NOT be pre-scaled; normalizer handles scaling)

The payload fixtures are the official examples of the futures/options WebSocket
market-data channels (trades, books), so a change in the documented shape shows up here.
"""

from __future__ import annotations

import copy
import enum
import json
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sdk(*, fresh_ws: bool = False) -> _FakeSdk:
    """Return a fake Fubon SDK whose futopt WebSocket client has the real 2.5.0 method signatures.

    With *fresh_ws* every ``init_realtime()`` after the first builds a new client object (a
    reconnect on a new socket); otherwise one client object is reused, so listeners registered
    by an earlier connection stay attached to it, which is the double-publish worst case.
    """
    return _FakeSdk(fresh_ws=fresh_ws)


def _make_runtime(sdk: _FakeSdk | None = None):
    from hft_platform.feed_adapter.fubon.quote_runtime import FubonQuoteRuntime

    return FubonQuoteRuntime(sdk or _make_sdk())


_MODE_ENV = "HFT_FUBON_REALTIME_MODE"


class _FakeMode(enum.Enum):
    """Mirror of ``fubon_neo.adapter.Mode`` as introspected from the fubon-neo 2.5.0 wheel."""

    Speed = "speed"
    Normal = "normal"


class _FakeFutOptWs:
    """Stand-in for ``sdk.marketdata.websocket_client.futopt`` of fubon-neo 2.5.0.

    Method signatures mirror the installed wheel: ``on(self, event, listener)``,
    ``connect(self)``, ``subscribe(self, params)``, ``unsubscribe(self, params)``,
    ``disconnect(self)``.  ``subscribe`` takes ONE dict (no ``on_message=`` keyword), and
    messages reach the "message" listeners as raw JSON text.
    """

    def __init__(self) -> None:
        self.listeners: dict[str, list[Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.subscribe_calls: list[dict[str, Any]] = []
        self.unsubscribe_calls: list[dict[str, Any]] = []
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.connect_error: Exception | None = None
        self.unsubscribe_error: Exception | None = None
        self.disconnect_error: Exception | None = None

    def on(self, event: str, listener: Any) -> None:
        self.calls.append(("on", event))
        self.listeners.setdefault(event, []).append(listener)

    def connect(self) -> None:
        self.calls.append(("connect", None))
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error

    def subscribe(self, params: dict[str, Any]) -> None:
        self.calls.append(("subscribe", params))
        self.subscribe_calls.append(params)

    def unsubscribe(self, params: dict[str, Any]) -> None:
        self.calls.append(("unsubscribe", params))
        self.unsubscribe_calls.append(params)
        if self.unsubscribe_error is not None:
            raise self.unsubscribe_error

    def disconnect(self) -> None:
        self.calls.append(("disconnect", None))
        self.disconnect_calls += 1
        if self.disconnect_error is not None:
            raise self.disconnect_error

    def deliver(self, raw: Any) -> None:
        """Hand one raw message to every "message" listener, as the SDK's socket thread does."""
        for listener in list(self.listeners.get("message", [])):
            listener(raw)

    def ack_all(self) -> None:
        """Answer every recorded subscribe with the service's ``subscribed`` event."""
        for params in list(self.subscribe_calls):
            self.deliver(_subscribed_msg(params["channel"], params["symbol"]))


class _NoStockClient:
    """The 2.5.0 stock client serves v1.0 only: any use by the futopt runtime is a bug."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"stock websocket client must not be used (accessed {name!r})")


class _FakeSdk:
    """``sdk.marketdata`` only exists after ``init_realtime()``, as in the real wheel."""

    def __init__(self, *, fresh_ws: bool = False) -> None:
        self.futopt_ws = _FakeFutOptWs()
        self.all_ws: list[_FakeFutOptWs] = [self.futopt_ws]
        self.fresh_ws = fresh_ws
        self.next_connect_error: Exception | None = None
        self.init_realtime_calls: list[tuple[Any, Any, Any]] = []

    def init_realtime(self, mode: Any = _FakeMode.Speed, version: Any = None, base_url: Any = None) -> None:
        self.init_realtime_calls.append((mode, version, base_url))
        if self.fresh_ws and len(self.init_realtime_calls) > 1:
            self.futopt_ws = _FakeFutOptWs()
            self.all_ws.append(self.futopt_ws)
        if self.next_connect_error is not None:
            self.futopt_ws.connect_error = self.next_connect_error
        self.marketdata = SimpleNamespace(
            websocket_client=SimpleNamespace(stock=_NoStockClient(), futopt=self.futopt_ws),
        )


def _sub_id(channel: str, symbol: str) -> str:
    return f"id-{channel}-{symbol}"


def _envelope(event: str, **fields: Any) -> str:
    return json.dumps({"event": event, **fields})


def _subscribed_msg(channel: str, symbol: str) -> str:
    return _envelope("subscribed", data={"id": _sub_id(channel, symbol), "channel": channel, "symbol": symbol})


def _data_msg(channel: str, payload: dict[str, Any], **envelope: Any) -> str:
    return _envelope("data", data=payload, channel=channel, **envelope)


@pytest.fixture(autouse=True)
def _fubon_sdk_mode(monkeypatch: pytest.MonkeyPatch) -> type[_FakeMode]:
    """Stand-in for ``fubon_neo.sdk`` (the SDK is an optional extra) and a clean operator env."""
    monkeypatch.delenv(_MODE_ENV, raising=False)
    sdk_module = types.ModuleType("fubon_neo.sdk")
    sdk_module.Mode = _FakeMode
    monkeypatch.setitem(sys.modules, "fubon_neo.sdk", sdk_module)
    return _FakeMode


@pytest.fixture()
def metrics():
    """A fresh MetricsRegistry so counter assertions start from the born-at-zero state."""
    from hft_platform.observability.metrics import MetricsRegistry

    MetricsRegistry._instance = None
    registry = MetricsRegistry.get()
    yield registry
    MetricsRegistry._instance = None


def _dropped(registry: Any, channel: str) -> float:
    return registry.fubon_trial_frames_dropped_total.labels(channel=channel)._value.get()


# ---------------------------------------------------------------------------
# Documented payloads
# ---------------------------------------------------------------------------
# The data objects below are the official examples of the futures/options WebSocket
# market-data channels.  Prices are plain numbers and time is epoch MICROSECONDS (the
# pages do not state the unit; it is inferred from the example values, e.g.
# 1702956487023000 = 2023-12-19 11:28:07.023 Asia/Taipei, inside the day session).

DOC_TRADE_DATA: dict[str, Any] = {
    "symbol": "TXFA4",
    "type": "FUTURE",
    "exchange": "TAIFEX",
    "trades": [{"price": 17540, "size": 1, "bid": 17539, "ask": 17540}],
    "total": {"tradeVolume": 12174, "totalBidMatch": 8760, "totalAskMatch": 7907},
    "time": 1702956487023000,
    "serial": 159250,
}
DOC_TRADE_TS_NS = 1_702_956_487_023_000_000

# The documented trial-matching example (pre-open night session): trades[0] carries a trial
# match price, the totals are 0 and isTrial is true.
DOC_TRIAL_TRADE_DATA: dict[str, Any] = {
    "symbol": "TXFF6",
    "type": "FUTURE_AH",
    "exchange": "TAIFEX",
    "trades": [{"price": 45380, "size": 1}],
    "total": {"tradeVolume": 0, "totalBidMatch": 0, "totalAskMatch": 0},
    "isTrial": True,
    "time": 1781506205000000,
    "serial": 3,
}

DOC_BOOK_DATA: dict[str, Any] = {
    "symbol": "TXFA4",
    "type": "FUTURE",
    "exchange": "TAIFEX",
    "bids": [
        {"price": 17539, "size": 2},
        {"price": 17538, "size": 4},
        {"price": 17537, "size": 3},
        {"price": 17536, "size": 10},
        {"price": 17535, "size": 10},
    ],
    "asks": [
        {"price": 17541, "size": 2},
        {"price": 17542, "size": 15},
        {"price": 17543, "size": 3},
        {"price": 17544, "size": 5},
        {"price": 17545, "size": 4},
    ],
    "derivedBid": {"price": 17540, "size": 1},
    "derivedAsk": {"price": 0, "size": 0},
    "time": 1702956500113000,
}
DOC_BOOK_TS_NS = 1_702_956_500_113_000_000

# What the runtime must hand to the normalizer for the documented examples.
DOC_TICK: dict[str, Any] = {
    "code": "TXFA4",
    "close": 17540,
    "volume": 1,
    "total_volume": 12174,
    "ts": DOC_TRADE_TS_NS,
}
DOC_BOOK_OUT: dict[str, Any] = {
    "code": "TXFA4",
    "bid_price": [17539, 17538, 17537, 17536, 17535],
    "bid_volume": [2, 4, 3, 10, 10],
    "ask_price": [17541, 17542, 17543, 17544, 17545],
    "ask_volume": [2, 15, 3, 5, 4],
    "ts": DOC_BOOK_TS_NS,
}

_ABSENT = object()


def _trade_frame(**extra: Any) -> dict[str, Any]:
    """The documented trades data object, with fields overridden by *extra*."""
    return {**copy.deepcopy(DOC_TRADE_DATA), **extra}


def _book_frame(**extra: Any) -> dict[str, Any]:
    """The documented books data object, with fields overridden by *extra*."""
    return {**copy.deepcopy(DOC_BOOK_DATA), **extra}


def _trade_entry(price: Any, size: Any = 1) -> list[dict[str, Any]]:
    return [{"price": price, "size": size}]


def _levels(*pairs: tuple[Any, Any]) -> list[dict[str, Any]]:
    return [{"price": price, "size": size} for price, size in pairs]


def _runtime_with_mocks() -> tuple[Any, MagicMock, MagicMock]:
    """A runtime with mocked callbacks and logger, for tests that inspect drops."""
    rt = _make_runtime()
    rt.log = MagicMock()
    on_tick = MagicMock()
    on_bidask = MagicMock()
    rt.register_quote_callbacks(on_tick=on_tick, on_bidask=on_bidask)
    return rt, on_tick, on_bidask


def _reasons(rt: Any) -> list[str]:
    """Reasons of the 'message dropped' warnings logged by a runtime built by _runtime_with_mocks."""
    return [call.kwargs["reason"] for call in rt.log.warning.call_args_list]


# ---------------------------------------------------------------------------
# Trade callback translation
# ---------------------------------------------------------------------------


class TestTradeCallback:
    def test_documented_trade_example_translates_to_canonical_tick(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        rt._on_fubon_trade(DOC_TRADE_DATA)

        assert received == [DOC_TICK]

    def test_documented_trade_envelope_through_the_message_listener(self, metrics: Any) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        rt._on_ws_message(_data_msg("trades", DOC_TRADE_DATA, id="<CHANNEL_ID>"))

        assert received == [DOC_TICK]

    def test_trade_time_is_epoch_microseconds_converted_to_nanoseconds(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        rt._on_fubon_trade(DOC_TRADE_DATA)

        ts_ns = received[0]["ts"]
        assert ts_ns == 1_702_956_487_023_000_000
        taipei = timezone(timedelta(hours=8))
        local = datetime.fromtimestamp(ts_ns // 1_000_000_000, taipei)
        assert local.strftime("%Y-%m-%d %H:%M:%S") == "2023-12-19 11:28:07"
        assert ts_ns % 1_000_000_000 == 23_000_000

    @pytest.mark.parametrize("time_us", [10**14, 10**17 - 1, 1702956487023000.0])
    def test_trade_time_inside_the_microsecond_range_is_accepted(self, time_us: float) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        rt._on_fubon_trade(_trade_frame(time=time_us))

        assert [tick["ts"] for tick in received] == [int(time_us) * 1_000]

    def test_trade_price_and_size_come_from_the_first_trades_entry(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        rt._on_fubon_trade(_trade_frame(trades=[{"price": 17545, "size": 3, "bid": 17544, "ask": 17545}]))

        assert received[0]["close"] == 17545
        assert received[0]["volume"] == 3

    def test_trade_bid_ask_serial_and_match_counts_are_not_forwarded(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        rt._on_fubon_trade(DOC_TRADE_DATA)

        assert set(received[0]) == {"code", "close", "volume", "total_volume", "ts"}

    def test_total_volume_is_the_documented_total_trade_volume(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        rt._on_fubon_trade(_trade_frame(total={"tradeVolume": 777, "totalBidMatch": 1, "totalAskMatch": 2}))

        assert received[0]["total_volume"] == 777

    @pytest.mark.parametrize(
        "total",
        [_ABSENT, None, {}, [], "x", {"tradeVolume": None}, {"tradeVolume": "5"}, {"tradeVolume": True}],
    )
    def test_missing_or_invalid_total_volume_is_zero_and_the_trade_is_forwarded(self, total: Any) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())
        data = _trade_frame()
        if total is _ABSENT:
            del data["total"]
        else:
            data["total"] = total

        rt._on_fubon_trade(data)

        assert len(received) == 1
        assert received[0]["total_volume"] == 0
        assert received[0]["close"] == 17540

    def test_trade_size_is_forwarded_as_int(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        rt._on_fubon_trade(_trade_frame(trades=_trade_entry(17540, 2.0)))

        assert received[0]["volume"] == 2
        assert isinstance(received[0]["volume"], int)

    def test_extra_trade_entries_forward_only_the_first_and_are_noted(self) -> None:
        rt, on_tick, _ = _runtime_with_mocks()

        rt._on_fubon_trade(_trade_frame(trades=[{"price": 17540, "size": 1}, {"price": 17541, "size": 2}]))

        assert on_tick.call_count == 1
        assert on_tick.call_args.args[0]["close"] == 17540
        assert _reasons(rt) == ["trade_extra_entries_ignored"]

    def test_single_trade_entry_is_not_noted(self) -> None:
        rt, on_tick, _ = _runtime_with_mocks()

        rt._on_fubon_trade(DOC_TRADE_DATA)

        assert on_tick.call_count == 1
        assert _reasons(rt) == []

    def test_trade_no_callback_noop(self) -> None:
        """No crash and no state change when on_tick is not registered."""
        rt = _make_runtime()

        result = rt._on_fubon_trade(DOC_TRADE_DATA)

        assert result is None
        assert rt._last_data_ts == 0.0
        assert rt._tick_buffer["code"] == ""

    def test_trade_callback_error_is_logged_and_does_not_escape(self) -> None:
        rt, on_tick, _ = _runtime_with_mocks()
        on_tick.side_effect = RuntimeError("downstream failed")

        result = rt._on_fubon_trade(DOC_TRADE_DATA)

        assert result is None
        assert rt.log.error.call_count == 1
        assert rt.log.error.call_args.kwargs["error"] == "downstream failed"

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"symbol": None}, "trade_symbol_missing"),
            ({"symbol": ""}, "trade_symbol_missing"),
            ({"symbol": 2330}, "trade_symbol_missing"),
            ({"trades": []}, "trade_entry_missing"),
            ({"trades": None}, "trade_entry_missing"),
            ({"trades": "x"}, "trade_entry_missing"),
            ({"trades": {"price": 17540, "size": 1}}, "trade_entry_missing"),
            ({"trades": [None]}, "trade_entry_missing"),
            ({"trades": [5]}, "trade_entry_missing"),
            ({"trades": [{"size": 1}]}, "trade_price_or_size_invalid"),
            ({"trades": [{"price": 17540}]}, "trade_price_or_size_invalid"),
            ({"trades": _trade_entry("17540", 1)}, "trade_price_or_size_invalid"),
            ({"trades": _trade_entry(17540, None)}, "trade_price_or_size_invalid"),
            ({"trades": _trade_entry(True, 1)}, "trade_price_or_size_invalid"),
            ({"trades": _trade_entry(17540, True)}, "trade_price_or_size_invalid"),
            ({"trades": _trade_entry(float("nan"), 1)}, "trade_price_or_size_invalid"),
            ({"trades": _trade_entry(float("inf"), 1)}, "trade_price_or_size_invalid"),
            ({"time": None}, "trade_time_invalid"),
            ({"time": 0}, "trade_time_invalid"),
            ({"time": -1702956487023000}, "trade_time_invalid"),
            ({"time": "1702956487023000"}, "trade_time_invalid"),
            ({"time": 1702956487023}, "trade_time_invalid"),  # milliseconds
            ({"time": 1702956487023000000}, "trade_time_invalid"),  # nanoseconds
            ({"time": True}, "trade_time_invalid"),
            ({"time": float("nan")}, "trade_time_invalid"),
            ({"time": 10**14 - 1}, "trade_time_invalid"),
            ({"time": 10**17}, "trade_time_invalid"),
        ],
    )
    def test_malformed_trade_is_dropped_with_a_reason(self, overrides: dict[str, Any], reason: str) -> None:
        rt, on_tick, on_bidask = _runtime_with_mocks()

        rt._on_fubon_trade(_trade_frame(**overrides))

        assert on_tick.call_count == 0
        assert on_bidask.call_count == 0
        assert _reasons(rt) == [reason]
        assert rt.log.error.call_count == 0

    @pytest.mark.parametrize(
        ("missing", "reason"),
        [("symbol", "trade_symbol_missing"), ("trades", "trade_entry_missing"), ("time", "trade_time_invalid")],
    )
    def test_trade_missing_a_required_field_is_dropped_with_a_reason(self, missing: str, reason: str) -> None:
        rt, on_tick, _ = _runtime_with_mocks()
        data = _trade_frame()
        del data[missing]

        rt._on_fubon_trade(data)

        assert on_tick.call_count == 0
        assert _reasons(rt) == [reason]

    @pytest.mark.parametrize("payload", [None, [DOC_TRADE_DATA], "TXFA4", 5, SimpleNamespace(**DOC_TRADE_DATA)])
    def test_trade_payload_that_is_not_an_object_is_dropped(self, payload: Any) -> None:
        rt, on_tick, _ = _runtime_with_mocks()

        rt._on_fubon_trade(payload)

        assert on_tick.call_count == 0
        assert _reasons(rt) == ["trade_payload_not_object"]

    def test_rejected_trade_leaves_buffer_and_liveness_untouched(self) -> None:
        rt, _, _ = _runtime_with_mocks()

        rt._on_fubon_trade(_trade_frame(time=0))

        assert rt._tick_buffer == {"code": "", "close": 0, "volume": 0, "total_volume": 0, "ts": 0}
        assert rt._last_data_ts == 0.0

    def test_accepted_trade_refreshes_liveness(self) -> None:
        rt, _, _ = _runtime_with_mocks()
        before = time.monotonic()

        rt._on_fubon_trade(DOC_TRADE_DATA)

        assert rt._last_data_ts >= before


# ---------------------------------------------------------------------------
# Book callback translation
# ---------------------------------------------------------------------------


class TestBookCallback:
    def test_documented_book_example_translates_to_canonical_bidask(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(dict(d)))

        rt._on_fubon_book(DOC_BOOK_DATA)

        assert received == [DOC_BOOK_OUT]

    def test_documented_book_envelope_through_the_message_listener(self, metrics: Any) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(dict(d)))

        rt._on_ws_message(_data_msg("books", DOC_BOOK_DATA, id="<CHANNEL_ID>"))

        assert received == [DOC_BOOK_OUT]

    def test_book_time_is_epoch_microseconds_converted_to_nanoseconds(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(dict(d)))

        rt._on_fubon_book(DOC_BOOK_DATA)

        ts_ns = received[0]["ts"]
        assert ts_ns == 1_702_956_500_113_000_000
        taipei = timezone(timedelta(hours=8))
        local = datetime.fromtimestamp(ts_ns // 1_000_000_000, taipei)
        assert local.strftime("%Y-%m-%d %H:%M:%S") == "2023-12-19 11:28:20"
        assert ts_ns % 1_000_000_000 == 113_000_000

    @pytest.mark.parametrize(
        ("n_bids", "n_asks"),
        [(0, 0), (1, 0), (0, 1), (2, 1), (5, 3), (3, 5), (5, 5)],
    )
    def test_book_forwards_only_the_levels_present_without_zero_fill(self, n_bids: int, n_asks: int) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(dict(d)))
        bids = [(17539 - i, 2 + i) for i in range(n_bids)]
        asks = [(17541 + i, 3 + i) for i in range(n_asks)]

        rt._on_fubon_book(_book_frame(bids=_levels(*bids), asks=_levels(*asks)))

        assert received[0]["bid_price"] == [price for price, _ in bids]
        assert received[0]["bid_volume"] == [size for _, size in bids]
        assert received[0]["ask_price"] == [price for price, _ in asks]
        assert received[0]["ask_volume"] == [size for _, size in asks]

    def test_book_deeper_than_five_levels_forwards_the_best_five(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(dict(d)))
        deep_bids = _levels(*[(17539 - i, 1 + i) for i in range(7)])

        rt._on_fubon_book(_book_frame(bids=deep_bids))

        assert received[0]["bid_price"] == [17539, 17538, 17537, 17536, 17535]
        assert received[0]["bid_volume"] == [1, 2, 3, 4, 5]

    @pytest.mark.parametrize("side", [_ABSENT, None, []])
    def test_book_side_that_is_absent_or_empty_forwards_an_empty_side(self, side: Any) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(dict(d)))
        data = _book_frame()
        if side is _ABSENT:
            del data["asks"]
        else:
            data["asks"] = side

        rt._on_fubon_book(data)

        assert received[0]["ask_price"] == []
        assert received[0]["ask_volume"] == []
        assert received[0]["bid_price"] == DOC_BOOK_OUT["bid_price"]

    @pytest.mark.parametrize(
        "derived",
        [
            {"derivedBid": {"price": 99999, "size": 9}, "derivedAsk": {"price": 1, "size": 1}},
            {"derivedBid": "garbage", "derivedAsk": [1, 2]},
            {"derivedBid": None, "derivedAsk": None},
        ],
    )
    def test_derived_levels_are_ignored_whatever_they_hold(self, derived: dict[str, Any]) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(dict(d)))

        rt._on_fubon_book(_book_frame(**derived))

        assert received == [DOC_BOOK_OUT]

    def test_book_without_derived_fields_is_forwarded(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(dict(d)))
        data = _book_frame()
        del data["derivedBid"]
        del data["derivedAsk"]

        rt._on_fubon_book(data)

        assert received == [DOC_BOOK_OUT]

    def test_book_sizes_are_forwarded_as_int(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(dict(d)))

        rt._on_fubon_book(_book_frame(bids=_levels((17539, 2.0)), asks=_levels((17541, 3.0))))

        assert received[0]["bid_volume"] == [2]
        assert received[0]["ask_volume"] == [3]
        assert isinstance(received[0]["bid_volume"][0], int)
        assert isinstance(received[0]["ask_volume"][0], int)

    def test_book_negative_spread_prices_pass_through_unchanged(self) -> None:
        """Spread contracts can quote negative prices; the translator neither clamps nor drops them."""
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(dict(d)))

        rt._on_fubon_book(_book_frame(bids=_levels((-5, 2)), asks=_levels((-3, 4))))

        assert received[0]["bid_price"] == [-5]
        assert received[0]["ask_price"] == [-3]

    def test_book_no_callback_noop(self) -> None:
        rt = _make_runtime()

        result = rt._on_fubon_book(DOC_BOOK_DATA)

        assert result is None
        assert rt._last_data_ts == 0.0

    def test_book_callback_error_is_logged_and_does_not_escape(self) -> None:
        rt, _, on_bidask = _runtime_with_mocks()
        on_bidask.side_effect = RuntimeError("downstream failed")

        result = rt._on_fubon_book(DOC_BOOK_DATA)

        assert result is None
        assert rt.log.error.call_count == 1
        assert rt.log.error.call_args.kwargs["error"] == "downstream failed"

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"symbol": None}, "book_symbol_missing"),
            ({"symbol": ""}, "book_symbol_missing"),
            ({"symbol": 5}, "book_symbol_missing"),
            ({"bids": "x"}, "book_levels_malformed"),
            ({"bids": {"price": 17539, "size": 2}}, "book_levels_malformed"),
            ({"asks": 5}, "book_levels_malformed"),
            ({"bids": [None]}, "book_levels_malformed"),
            ({"bids": [5]}, "book_levels_malformed"),
            ({"bids": [{"size": 2}]}, "book_levels_malformed"),
            ({"bids": [{"price": 17539}]}, "book_levels_malformed"),
            ({"asks": _levels(("17541", 2))}, "book_levels_malformed"),
            ({"asks": _levels((17541, None))}, "book_levels_malformed"),
            ({"bids": _levels((True, 2))}, "book_levels_malformed"),
            ({"asks": _levels((float("nan"), 2))}, "book_levels_malformed"),
            ({"asks": _levels((float("inf"), 2))}, "book_levels_malformed"),
            ({"time": None}, "book_time_invalid"),
            ({"time": 0}, "book_time_invalid"),
            ({"time": "1702956500113000"}, "book_time_invalid"),
            ({"time": 1702956500113}, "book_time_invalid"),  # milliseconds
            ({"time": 1702956500113000000}, "book_time_invalid"),  # nanoseconds
            ({"time": 10**14 - 1}, "book_time_invalid"),
            ({"time": 10**17}, "book_time_invalid"),
        ],
    )
    def test_malformed_book_is_dropped_with_a_reason(self, overrides: dict[str, Any], reason: str) -> None:
        rt, on_tick, on_bidask = _runtime_with_mocks()

        rt._on_fubon_book(_book_frame(**overrides))

        assert on_bidask.call_count == 0
        assert on_tick.call_count == 0
        assert _reasons(rt) == [reason]
        assert rt.log.error.call_count == 0

    @pytest.mark.parametrize(
        ("missing", "reason"),
        [("symbol", "book_symbol_missing"), ("time", "book_time_invalid")],
    )
    def test_book_missing_a_required_field_is_dropped_with_a_reason(self, missing: str, reason: str) -> None:
        rt, _, on_bidask = _runtime_with_mocks()
        data = _book_frame()
        del data[missing]

        rt._on_fubon_book(data)

        assert on_bidask.call_count == 0
        assert _reasons(rt) == [reason]

    @pytest.mark.parametrize("payload", [None, [DOC_BOOK_DATA], "TXFA4", 5, SimpleNamespace(**DOC_BOOK_DATA)])
    def test_book_payload_that_is_not_an_object_is_dropped(self, payload: Any) -> None:
        rt, _, on_bidask = _runtime_with_mocks()

        rt._on_fubon_book(payload)

        assert on_bidask.call_count == 0
        assert _reasons(rt) == ["book_payload_not_object"]

    def test_accepted_book_refreshes_liveness_and_rejected_book_does_not(self) -> None:
        rt, _, _ = _runtime_with_mocks()
        before = time.monotonic()

        rt._on_fubon_book(_book_frame(time=0))
        assert rt._last_data_ts == 0.0

        rt._on_fubon_book(DOC_BOOK_DATA)
        assert rt._last_data_ts >= before


# ---------------------------------------------------------------------------
# Undocumented shapes are not guessed at
# ---------------------------------------------------------------------------


class TestUndocumentedShapes:
    def test_legacy_flat_trade_payload_is_dropped_not_guessed(self) -> None:
        rt, on_tick, _ = _runtime_with_mocks()

        rt._on_fubon_trade({"symbol": "2330", "close": 595.0, "volume": 3, "datetime": 1_700_000_000_000_000_000})

        assert on_tick.call_count == 0
        assert _reasons(rt) == ["trade_entry_missing"]

    def test_legacy_flat_book_payload_is_dropped_not_guessed(self) -> None:
        rt, _, on_bidask = _runtime_with_mocks()

        rt._on_fubon_book(
            {
                "symbol": "2330",
                "bid_prices": [595.0],
                "bid_sizes": [10],
                "ask_prices": [596.0],
                "ask_sizes": [5],
                "datetime": 1_700_000_000_000_000_000,
            }
        )

        assert on_bidask.call_count == 0
        assert _reasons(rt) == ["book_time_invalid"]

    def test_trade_with_top_level_price_and_size_is_dropped_not_guessed(self) -> None:
        rt, on_tick, _ = _runtime_with_mocks()

        rt._on_fubon_trade({"symbol": "TXFA4", "price": 17540, "size": 1, "time": 1702956487023000})

        assert on_tick.call_count == 0
        assert _reasons(rt) == ["trade_entry_missing"]

    def test_object_style_trade_payload_is_dropped_not_read_by_attribute(self) -> None:
        rt, on_tick, _ = _runtime_with_mocks()
        obj = MagicMock()
        obj.symbol = "TXFA4"
        obj.trades = _trade_entry(17540)
        obj.time = 1702956487023000

        rt._on_fubon_trade(obj)

        assert on_tick.call_count == 0
        assert _reasons(rt) == ["trade_payload_not_object"]


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

        rt._on_fubon_trade(_trade_frame(symbol="TXFA4", trades=_trade_entry(17540, 1)))
        rt._on_fubon_trade(_trade_frame(symbol="TXFB4", trades=_trade_entry(17600, 2)))

        assert len(received) == 2
        assert received[0] is not received[1], "each tick callback must receive a fresh snapshot dict"

    def test_tick_callback_never_receives_the_workspace_buffer_itself(self) -> None:
        rt = _make_runtime()
        received: list[dict] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(d), on_bidask=MagicMock())
        workspace = rt._tick_buffer

        rt._on_fubon_trade(DOC_TRADE_DATA)

        assert rt._tick_buffer is workspace, "the workspace is allocated once and reused"
        assert received[0] is not workspace
        assert received[0] == DOC_TICK

    def test_bidask_callback_receives_distinct_dicts_and_lists(self) -> None:
        """Each bidask callback invocation must receive a *distinct* dict and distinct level lists.

        We store the dict objects themselves (not just their ids) to prevent
        premature GC from causing id() reuse between callbacks.
        """
        rt = _make_runtime()
        received: list[dict] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(d))

        rt._on_fubon_book(DOC_BOOK_DATA)
        rt._on_fubon_book(DOC_BOOK_DATA)

        assert len(received) == 2
        assert received[0] is not received[1], "each bidask callback must receive a fresh snapshot dict"
        for key in ("bid_price", "bid_volume", "ask_price", "ask_volume"):
            assert received[0][key] is not received[1][key], f"{key} list must not be shared between callbacks"

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

        rt._on_fubon_trade(_trade_frame(symbol="TXFA4", trades=_trade_entry(17540, 1)))
        rt._on_fubon_trade(_trade_frame(symbol="TXFB4", trades=_trade_entry(17600, 2)))

        assert len(stored) == 2
        # First snapshot must still reflect the first trade.
        assert stored[0]["code"] == "TXFA4"
        assert stored[0]["close"] == 17540  # raw price
        assert stored[0]["volume"] == 1
        # Second snapshot must reflect the second trade.
        assert stored[1]["code"] == "TXFB4"
        assert stored[1]["close"] == 17600  # raw price
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

        rt._on_fubon_book(_book_frame(symbol="TXFA4"))
        rt._on_fubon_book(_book_frame(symbol="TXFB4", bids=_levels((17599, 1)), asks=_levels((17601, 1), (17602, 2))))

        assert len(stored) == 2
        # First snapshot's lists must still contain the first book.
        assert stored[0]["code"] == "TXFA4"
        assert stored[0]["bid_price"] == DOC_BOOK_OUT["bid_price"]
        assert stored[0]["ask_price"] == DOC_BOOK_OUT["ask_price"]
        assert stored[0]["ask_volume"] == DOC_BOOK_OUT["ask_volume"]
        # Second snapshot must contain the second book (shallower: nothing carried over from the first).
        assert stored[1]["code"] == "TXFB4"
        assert stored[1]["bid_price"] == [17599]
        assert stored[1]["ask_price"] == [17601, 17602]
        assert stored[1]["ask_volume"] == [1, 2]


# ---------------------------------------------------------------------------
# Subscribe / unsubscribe
# ---------------------------------------------------------------------------


class TestSubscription:
    def test_subscribe_calls_sdk(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["2330", "2317"])

        assert len(sdk.init_realtime_calls) == 1
        # 2 symbols x 2 channels = 4 subscribe calls
        assert len(sdk.futopt_ws.subscribe_calls) == 4
        assert rt._subscribed == {"2330", "2317"}

    def test_subscribe_idempotent(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["2330"])
        rt.subscribe(["2330"])  # duplicate

        assert len(sdk.futopt_ws.subscribe_calls) == 2  # only first subscription

    def test_init_realtime_called_once(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["2330"])
        rt.subscribe(["2317"])

        assert len(sdk.init_realtime_calls) == 1

    def test_unsubscribe_calls_sdk(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["2330"])
        sdk.futopt_ws.ack_all()
        rt.unsubscribe(["2330"])

        assert len(sdk.futopt_ws.unsubscribe_calls) == 2  # trades + books
        assert rt._subscribed == set()

    def test_unsubscribe_unknown_symbol_noop(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)
        rt.unsubscribe(["9999"])

        assert sdk.futopt_ws.unsubscribe_calls == []
        assert not hasattr(sdk, "marketdata")  # nothing was initialised


# ---------------------------------------------------------------------------
# WebSocket wiring (fubon-neo 2.5.0 futopt client)
# ---------------------------------------------------------------------------


class TestWebSocketWiring:
    def test_subscribe_uses_futopt_client_and_never_the_stock_client(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["TXFA4"])
        rt.stop()

        # The stock client raises AssertionError on any attribute access, so reaching
        # this line proves the stock (v1.0 only) client was never touched.
        assert len(sdk.futopt_ws.subscribe_calls) == 2

    def test_subscribe_creates_marketdata_via_init_realtime_before_using_it(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)
        assert not hasattr(sdk, "marketdata")

        rt.subscribe(["TXFA4"])

        assert len(sdk.init_realtime_calls) == 1
        assert hasattr(sdk, "marketdata")

    def test_subscribe_registers_listeners_then_connects_then_subscribes(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["TXFA4"])

        names = [name for name, _ in sdk.futopt_ws.calls]
        assert names == ["on", "on", "connect", "subscribe", "subscribe"]
        assert {arg for name, arg in sdk.futopt_ws.calls if name == "on"} == {"message", "error"}
        assert len(sdk.futopt_ws.listeners["message"]) == 1
        assert len(sdk.futopt_ws.listeners["error"]) == 1

    def test_registered_message_listener_feeds_the_registered_callbacks(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())
        rt.subscribe(["TXFA4"])

        sdk.futopt_ws.deliver(_data_msg("trades", DOC_TRADE_DATA))

        assert received == [DOC_TICK]

    def test_second_subscribe_does_not_reinitialise_reregister_or_reconnect(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["TXFA4"])
        rt.subscribe(["TXFB4"])

        ws = sdk.futopt_ws
        assert len(sdk.init_realtime_calls) == 1
        assert ws.connect_calls == 1
        assert len(ws.listeners["message"]) == 1
        assert len(ws.listeners["error"]) == 1
        assert len(ws.subscribe_calls) == 4

    def test_subscribe_sends_one_params_dict_per_channel(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["TXFA4"])

        assert sdk.futopt_ws.subscribe_calls == [
            {"channel": "trades", "symbol": "TXFA4"},
            {"channel": "books", "symbol": "TXFA4"},
        ]

    def test_subscribe_after_hours_adds_flag_to_both_channels(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["TXFA4"], after_hours=True)

        assert sdk.futopt_ws.subscribe_calls == [
            {"channel": "trades", "symbol": "TXFA4", "afterHours": True},
            {"channel": "books", "symbol": "TXFA4", "afterHours": True},
        ]

    def test_subscribe_day_session_omits_after_hours_key(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["TXFA4"], after_hours=False)

        assert all("afterHours" not in params for params in sdk.futopt_ws.subscribe_calls)

    def test_subscribe_skips_symbol_already_subscribed_whatever_the_session(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["TXFA4"])
        rt.subscribe(["TXFA4"], after_hours=True)

        assert len(sdk.futopt_ws.subscribe_calls) == 2
        assert all("afterHours" not in params for params in sdk.futopt_ws.subscribe_calls)

    def test_connect_failure_propagates_and_leaves_runtime_uninitialised(self) -> None:
        sdk = _make_sdk()
        sdk.futopt_ws.connect_error = RuntimeError("authentication failed")
        rt = _make_runtime(sdk)

        with pytest.raises(RuntimeError, match="authentication failed"):
            rt.subscribe(["TXFA4"])

        assert rt._ws is None
        assert rt._running is False
        assert rt._subscribed == set()
        assert sdk.futopt_ws.subscribe_calls == []

    def test_subscribe_after_failed_connect_connects_again(self) -> None:
        sdk = _make_sdk()
        sdk.futopt_ws.connect_error = RuntimeError("authentication failed")
        rt = _make_runtime(sdk)
        with pytest.raises(RuntimeError):
            rt.subscribe(["TXFA4"])
        sdk.futopt_ws.connect_error = None

        rt.subscribe(["TXFA4"])

        assert sdk.futopt_ws.connect_calls == 2
        assert rt._subscribed == {"TXFA4"}
        assert len(sdk.futopt_ws.subscribe_calls) == 2


# ---------------------------------------------------------------------------
# Raw message parsing and routing
# ---------------------------------------------------------------------------


def _subscribed_runtime(sdk: _FakeSdk | None = None, symbols: tuple[str, ...] = ("TXFA4",)) -> tuple[Any, _FakeSdk]:
    sdk = sdk or _make_sdk()
    rt = _make_runtime(sdk)
    rt.subscribe(list(symbols))
    return rt, sdk


class TestMessageRouting:
    def test_trades_data_message_reaches_tick_callback(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        sdk.futopt_ws.deliver(_data_msg("trades", _trade_frame()))

        assert received == [DOC_TICK]
        assert _dropped(metrics, "trades") == 0.0

    def test_books_data_message_reaches_bidask_callback(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()
        received: list[dict[str, Any]] = []
        on_tick = MagicMock()
        rt.register_quote_callbacks(
            on_tick=on_tick,
            on_bidask=lambda d: received.append({"code": d["code"], "bid_price": list(d["bid_price"])}),
        )

        sdk.futopt_ws.deliver(_data_msg("books", _book_frame()))

        assert received == [{"code": "TXFA4", "bid_price": DOC_BOOK_OUT["bid_price"]}]
        assert on_tick.call_count == 0
        assert _dropped(metrics, "books") == 0.0

    def test_data_message_is_routed_by_channel_not_by_payload_shape(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()
        on_tick = MagicMock()
        on_bidask = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=on_bidask)

        sdk.futopt_ws.deliver(_data_msg("trades", _trade_frame()))

        assert on_tick.call_count == 1
        assert on_bidask.call_count == 0

    def test_bytes_message_is_parsed_like_text(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()
        on_tick = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=MagicMock())

        sdk.futopt_ws.deliver(_data_msg("trades", _trade_frame()).encode())

        assert on_tick.call_count == 1

    def test_data_message_without_channel_is_routed_by_subscription_id(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()
        sdk.futopt_ws.ack_all()
        on_tick = MagicMock()
        on_bidask = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=on_bidask)

        sdk.futopt_ws.deliver(_envelope("data", id=_sub_id("trades", "TXFA4"), data=_trade_frame()))
        sdk.futopt_ws.deliver(_envelope("data", id=_sub_id("books", "TXFA4"), data=_book_frame()))

        assert on_tick.call_count == 1
        assert on_bidask.call_count == 1

    def test_data_message_with_unknown_channel_is_dropped_and_warned(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()
        rt.log = MagicMock()
        on_tick = MagicMock()
        on_bidask = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=on_bidask)

        sdk.futopt_ws.deliver(_data_msg("candles", _trade_frame()))
        sdk.futopt_ws.deliver(_envelope("data", data=_trade_frame()))  # no channel, no id

        assert on_tick.call_count == 0
        assert on_bidask.call_count == 0
        assert rt.log.warning.call_args_list[0].kwargs["reason"] == "data_channel_unknown"

    def test_data_message_with_non_object_payload_is_dropped_and_warned(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()
        rt.log = MagicMock()
        on_tick = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=MagicMock())

        sdk.futopt_ws.deliver(_envelope("data", channel="trades", data=[1, 2, 3]))
        sdk.futopt_ws.deliver(_envelope("data", channel="trades"))

        assert on_tick.call_count == 0
        assert rt.log.warning.call_args_list[0].kwargs["reason"] == "data_payload_not_object"

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            ("not json {", "message_malformed_json"),
            ("", "message_malformed_json"),
            ("[1, 2]", "message_not_json_object"),
            ("42", "message_not_json_object"),
            ("null", "message_not_json_object"),
            (None, "message_not_json_object"),
            (12345, "message_not_json_object"),
            ({"event": "data"}, "message_not_json_object"),
        ],
    )
    def test_unparseable_message_is_dropped_without_raising(self, metrics: Any, raw: Any, reason: str) -> None:
        rt, sdk = _subscribed_runtime()
        rt.log = MagicMock()
        on_tick = MagicMock()
        on_bidask = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=on_bidask)

        sdk.futopt_ws.deliver(raw)

        assert on_tick.call_count == 0
        assert on_bidask.call_count == 0
        assert rt.log.warning.call_args_list[0].kwargs["reason"] == reason

    @pytest.mark.parametrize("event", ["authenticated", "heartbeat", "pong", "subscriptions", "somethingnew"])
    def test_non_data_events_need_no_action_and_do_not_log_problems(self, metrics: Any, event: str) -> None:
        rt, sdk = _subscribed_runtime()
        rt.log = MagicMock()
        on_tick = MagicMock()
        on_bidask = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=on_bidask)

        sdk.futopt_ws.deliver(_envelope(event, data={"time": 1}))

        assert on_tick.call_count == 0
        assert on_bidask.call_count == 0
        assert rt.log.warning.call_count == 0
        assert rt.log.error.call_count == 0

    def test_repeated_unparseable_messages_are_logged_first_then_every_thousandth(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()
        rt.log = MagicMock()

        for _ in range(2001):
            sdk.futopt_ws.deliver("not json {")

        counts = [call.kwargs["count"] for call in rt.log.warning.call_args_list]
        assert counts == [1, 1000, 2000]

    def test_malformed_trade_message_is_dropped_noted_and_not_counted_as_trial(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()
        rt.log = MagicMock()
        on_tick = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=MagicMock())

        sdk.futopt_ws.deliver(_data_msg("trades", _trade_frame(time=1702956487023)))  # milliseconds

        assert on_tick.call_count == 0
        assert _reasons(rt) == ["trade_time_invalid"]
        assert _dropped(metrics, "trades") == 0.0

    def test_malformed_book_message_is_dropped_noted_and_not_counted_as_trial(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()
        rt.log = MagicMock()
        on_bidask = MagicMock()
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=on_bidask)

        sdk.futopt_ws.deliver(_data_msg("books", _book_frame(bids="not a list")))

        assert on_bidask.call_count == 0
        assert _reasons(rt) == ["book_levels_malformed"]
        assert _dropped(metrics, "books") == 0.0

    def test_message_after_stop_is_ignored(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()
        on_tick = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=MagicMock())
        rt.stop()

        # The SDK keeps the bound listener after stop(); a late frame must not be published.
        sdk.futopt_ws.deliver(_data_msg("trades", _trade_frame()))
        sdk.futopt_ws.deliver(_data_msg("trades", _trade_frame(), isTrial=True))

        assert on_tick.call_count == 0
        assert _dropped(metrics, "trades") == 0.0

    def test_handler_failure_is_logged_and_does_not_escape_to_the_sdk_thread(
        self, metrics: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hft_platform.feed_adapter.fubon.quote_runtime import FubonQuoteRuntime

        def boom(self: Any, msg: Any) -> None:
            raise RuntimeError("router bug")

        monkeypatch.setattr(FubonQuoteRuntime, "_route_data", boom)
        rt, sdk = _subscribed_runtime()
        rt.log = MagicMock()

        sdk.futopt_ws.deliver(_data_msg("trades", _trade_frame()))

        assert rt.log.error.call_count == 1
        assert rt.log.error.call_args.kwargs["error"] == "router bug"

    def test_message_delivered_from_another_thread_reaches_callback_on_that_thread(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()
        seen: list[int] = []
        rt.register_quote_callbacks(on_tick=lambda d: seen.append(threading.get_ident()), on_bidask=MagicMock())
        worker = threading.Thread(target=sdk.futopt_ws.deliver, args=(_data_msg("trades", _trade_frame()),))

        worker.start()
        worker.join(timeout=5.0)

        # quote_runtime never touches the event loop: the registered callback runs on the SDK thread
        # and is itself responsible for call_soon_threadsafe.
        assert seen == [worker.ident]


# ---------------------------------------------------------------------------
# Subscription ids
# ---------------------------------------------------------------------------


class TestSubscriptionIds:
    def test_unsubscribe_sends_the_ids_from_subscribed_events(self) -> None:
        rt, sdk = _subscribed_runtime()
        sdk.futopt_ws.ack_all()

        rt.unsubscribe(["TXFA4"])

        assert sdk.futopt_ws.unsubscribe_calls == [
            {"id": _sub_id("trades", "TXFA4")},
            {"id": _sub_id("books", "TXFA4")},
        ]
        assert rt._subscribed == set()

    def test_unsubscribe_never_sends_channel_or_symbol(self) -> None:
        rt, sdk = _subscribed_runtime()
        sdk.futopt_ws.ack_all()

        rt.unsubscribe(["TXFA4"])

        assert all(set(params) == {"id"} for params in sdk.futopt_ws.unsubscribe_calls)

    def test_subscribed_event_with_list_data_is_tracked(self) -> None:
        rt, sdk = _subscribed_runtime()
        entries = [
            {"id": "t-1", "channel": "trades", "symbol": "TXFA4"},
            {"id": "b-1", "channel": "books", "symbol": "TXFA4"},
        ]
        sdk.futopt_ws.deliver(_envelope("subscribed", data=entries))

        rt.unsubscribe(["TXFA4"])

        assert sdk.futopt_ws.unsubscribe_calls == [{"id": "t-1"}, {"id": "b-1"}]

    def test_ids_are_tracked_per_symbol(self) -> None:
        rt, sdk = _subscribed_runtime(symbols=("TXFA4", "TMFA4"))
        sdk.futopt_ws.ack_all()

        rt.unsubscribe(["TMFA4"])

        assert sdk.futopt_ws.unsubscribe_calls == [
            {"id": _sub_id("trades", "TMFA4")},
            {"id": _sub_id("books", "TMFA4")},
        ]
        assert rt._subscribed == {"TXFA4"}

    def test_unsubscribe_without_acknowledgement_skips_the_sdk_call_and_forgets_the_symbol(self) -> None:
        rt, sdk = _subscribed_runtime()
        rt.log = MagicMock()

        rt.unsubscribe(["TXFA4"])

        assert sdk.futopt_ws.unsubscribe_calls == []
        assert rt._subscribed == set()
        assert [c.kwargs["channel"] for c in rt.log.warning.call_args_list] == ["trades", "books"]

    def test_unsubscribe_only_unacknowledged_channel_is_skipped(self) -> None:
        rt, sdk = _subscribed_runtime()
        sdk.futopt_ws.deliver(_subscribed_msg("books", "TXFA4"))  # trades never acknowledged

        rt.unsubscribe(["TXFA4"])

        assert sdk.futopt_ws.unsubscribe_calls == [{"id": _sub_id("books", "TXFA4")}]

    def test_unsubscribe_continues_when_the_sdk_call_raises(self) -> None:
        rt, sdk = _subscribed_runtime()
        sdk.futopt_ws.ack_all()
        sdk.futopt_ws.unsubscribe_error = RuntimeError("socket closed")

        rt.unsubscribe(["TXFA4"])

        assert len(sdk.futopt_ws.unsubscribe_calls) == 2  # books still attempted after trades raised
        assert rt._subscribed == set()

    def test_unsubscribed_event_forgets_the_id(self) -> None:
        rt, sdk = _subscribed_runtime()
        sdk.futopt_ws.ack_all()
        sdk.futopt_ws.deliver(_envelope("unsubscribed", data={"id": _sub_id("trades", "TXFA4")}))

        rt.unsubscribe(["TXFA4"])

        assert sdk.futopt_ws.unsubscribe_calls == [{"id": _sub_id("books", "TXFA4")}]

    def test_repeated_acknowledgement_replaces_the_id_and_drops_the_stale_one(self) -> None:
        rt, sdk = _subscribed_runtime()
        sdk.futopt_ws.deliver(_envelope("subscribed", data={"id": "old", "channel": "trades", "symbol": "TXFA4"}))
        sdk.futopt_ws.deliver(_envelope("subscribed", data={"id": "new", "channel": "trades", "symbol": "TXFA4"}))
        on_tick = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=MagicMock())

        sdk.futopt_ws.deliver(_envelope("data", id="old", data=_trade_frame()))  # stale id: unroutable
        sdk.futopt_ws.deliver(_envelope("data", id="new", data=_trade_frame()))
        rt.unsubscribe(["TXFA4"])

        assert on_tick.call_count == 1
        assert sdk.futopt_ws.unsubscribe_calls == [{"id": "new"}]

    def test_unsubscribed_event_for_a_replaced_id_keeps_the_current_id(self) -> None:
        rt, sdk = _subscribed_runtime()
        sdk.futopt_ws.deliver(_envelope("subscribed", data={"id": "old", "channel": "trades", "symbol": "TXFA4"}))
        sdk.futopt_ws.deliver(_envelope("subscribed", data={"id": "new", "channel": "trades", "symbol": "TXFA4"}))

        sdk.futopt_ws.deliver(_envelope("unsubscribed", data={"id": "old"}))
        rt.unsubscribe(["TXFA4"])

        assert sdk.futopt_ws.unsubscribe_calls == [{"id": "new"}]

    def test_stop_forgets_subscription_ids_of_the_closed_connection(self) -> None:
        rt, sdk = _subscribed_runtime()
        sdk.futopt_ws.ack_all()

        rt.stop()

        assert rt._sub_ids == {}
        assert rt._id_key == {}

    @pytest.mark.parametrize(
        "entry",
        [
            {"channel": "trades", "symbol": "TXFA4"},
            {"id": "", "channel": "trades", "symbol": "TXFA4"},
            {"id": 7, "channel": "trades", "symbol": "TXFA4"},
            {"id": "x", "symbol": "TXFA4"},
            {"id": "x", "channel": "trades"},
        ],
    )
    def test_malformed_subscribed_event_is_ignored(self, entry: dict[str, Any]) -> None:
        rt, sdk = _subscribed_runtime()
        rt.log = MagicMock()

        sdk.futopt_ws.deliver(_envelope("subscribed", data=entry))
        rt.unsubscribe(["TXFA4"])

        assert sdk.futopt_ws.unsubscribe_calls == []
        assert rt.log.warning.call_args_list[0].kwargs["reason"] == "subscribed_event_malformed"


# ---------------------------------------------------------------------------
# Error events
# ---------------------------------------------------------------------------


class TestErrorEvents:
    @pytest.mark.parametrize(
        ("code", "reason"),
        [(1001, "subscription_limit_exceeded"), (1003, "connection_limit_reached"), (4242, "error")],
    )
    def test_error_event_is_logged_with_code_and_reason(self, metrics: Any, code: int, reason: str) -> None:
        rt, sdk = _subscribed_runtime()
        rt.log = MagicMock()

        sdk.futopt_ws.deliver(_envelope("error", code=code, data={"message": "limit"}))

        assert rt.log.error.call_count == 1
        kwargs = rt.log.error.call_args.kwargs
        assert kwargs["code"] == code
        assert kwargs["reason"] == reason
        assert kwargs["message"] == "limit"

    def test_error_event_without_code_is_logged_as_generic_error(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()
        rt.log = MagicMock()

        sdk.futopt_ws.deliver(_envelope("error", data={"message": "Invalid authentication credentials"}))

        kwargs = rt.log.error.call_args.kwargs
        assert kwargs["code"] is None
        assert kwargs["reason"] == "error"

    def test_error_event_never_reaches_callbacks_or_the_trial_counter(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()
        on_tick = MagicMock()
        on_bidask = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=on_bidask)

        sdk.futopt_ws.deliver(_envelope("error", code=1001, data={"message": "Subscription limit exceeded"}))

        assert on_tick.call_count == 0
        assert on_bidask.call_count == 0
        assert _dropped(metrics, "trades") == 0.0
        assert _dropped(metrics, "books") == 0.0

    def test_error_event_logs_only_a_bounded_message(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()
        rt.log = MagicMock()

        sdk.futopt_ws.deliver(_envelope("error", code=1001, data={"message": "x" * 5000}))

        assert len(rt.log.error.call_args.kwargs["message"]) == 200

    def test_error_event_does_not_mark_subscriptions_failed_or_stop_the_runtime(self, metrics: Any) -> None:
        rt, sdk = _subscribed_runtime()

        sdk.futopt_ws.deliver(_envelope("error", code=1001, data={"message": "limit"}))

        assert rt._subscribed == {"TXFA4"}
        assert rt._stopped is False
        assert rt._running is True

    def test_sdk_error_listener_logs_transport_errors(self) -> None:
        rt, sdk = _subscribed_runtime()
        rt.log = MagicMock()

        for listener in sdk.futopt_ws.listeners["error"]:
            listener(ConnectionError("socket reset"))

        assert rt.log.error.call_count == 1
        assert rt.log.error.call_args.kwargs["error"] == "socket reset"


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
        sdk.futopt_ws.ack_all()
        rt.stop()

        # 2 symbols x 2 channels = 4 unsubscribe calls, each by subscription id
        assert len(sdk.futopt_ws.unsubscribe_calls) == 4
        assert all(set(params) == {"id"} for params in sdk.futopt_ws.unsubscribe_calls)
        assert len(rt._subscribed) == 0

    def test_stop_disconnects_the_websocket_once_after_unsubscribing(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)
        rt.subscribe(["2330"])
        sdk.futopt_ws.ack_all()

        rt.stop()
        rt.stop()

        names = [name for name, _ in sdk.futopt_ws.calls]
        assert names[-3:] == ["unsubscribe", "unsubscribe", "disconnect"]
        assert sdk.futopt_ws.disconnect_calls == 1
        assert rt._ws is None
        assert rt._running is False

    def test_stop_disconnect_failure_is_logged_and_does_not_raise(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)
        rt.subscribe(["2330"])
        sdk.futopt_ws.disconnect_error = RuntimeError("already closed")
        rt.log = MagicMock()

        rt.stop()

        assert sdk.futopt_ws.disconnect_calls == 1
        assert rt._ws is None
        assert rt.log.warning.call_args.kwargs["error"] == "already closed"

    def test_stop_before_any_subscribe_does_not_touch_the_sdk(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.stop()

        assert sdk.init_realtime_calls == []
        assert sdk.futopt_ws.calls == []

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
        rt._on_fubon_trade(_trade_frame())
        rt._on_fubon_book(_book_frame())
        assert len(tick_received) == 1
        assert len(book_received) == 1

        rt.stop()

        # Simulate the Fubon SDK delivering a buffered callback post-stop.
        rt._on_fubon_trade(_trade_frame(trades=_trade_entry(17600, 5)))
        rt._on_fubon_book(_book_frame(bids=_levels((17599, 50)), asks=_levels((17601, 50))))
        # No additional deliveries — the old runtime refuses to forward.
        assert len(tick_received) == 1, "late trade callback leaked through stopped runtime"
        assert len(book_received) == 1, "late book callback leaked through stopped runtime"


# ---------------------------------------------------------------------------
# Price passthrough (the normalizer scales; this module must not)
# ---------------------------------------------------------------------------


class TestPricePassthrough:
    def test_integer_price_is_forwarded_unscaled(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        rt._on_fubon_trade(_trade_frame(trades=_trade_entry(17540)))

        assert received[0]["close"] == 17540
        assert received[0]["close"] != 17540 * 10_000  # scaling happens once, in the normalizer

    @pytest.mark.parametrize("price", [0.0001, 12.5, 17540.5])
    def test_decimal_price_is_forwarded_unchanged(self, price: float) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        rt._on_fubon_trade(_trade_frame(trades=_trade_entry(price)))

        assert received[0]["close"] == price

    def test_book_prices_are_forwarded_unscaled(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(dict(d)))

        rt._on_fubon_book(DOC_BOOK_DATA)

        assert received[0]["bid_price"][0] == 17539
        assert received[0]["ask_price"][4] == 17545
        assert max(received[0]["ask_price"]) < 1_000_000

    def test_book_decimal_prices_are_forwarded_unchanged(self) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(dict(d)))

        rt._on_fubon_book(_book_frame(bids=_levels((12.5, 3)), asks=_levels((13.0, 4))))

        assert received[0]["bid_price"] == [12.5]
        assert received[0]["ask_price"] == [13.0]


# ---------------------------------------------------------------------------
# Consumer contract: the real normalizer
# ---------------------------------------------------------------------------


class TestNormalizerContract:
    @pytest.fixture
    def normalizer(self, tmp_path: Any, metrics: Any) -> Any:
        from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

        cfg = tmp_path / "symbols.yaml"
        cfg.write_text("symbols:\n  - code: 'TXFA4'\n    exchange: 'TAIFEX'\n    price_scale: 10000\n")
        return MarketDataNormalizer(str(cfg))

    @staticmethod
    def _levels_of(side: Any) -> list[list[int]]:
        return [[int(price), int(size)] for price, size in side]

    def test_documented_trade_is_scaled_exactly_once_by_the_normalizer(self, normalizer: Any) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())
        rt._on_fubon_trade(DOC_TRADE_DATA)

        event = normalizer.normalize_tick(received[0])

        assert event.price == 175_400_000  # 17540 x 10000, not x 10000 twice
        assert event.volume == 1
        assert event.total_volume == 12174
        assert event.meta.source_ts == DOC_TRADE_TS_NS

    def test_documented_book_is_scaled_exactly_once_on_every_level(self, normalizer: Any) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(dict(d)))
        rt._on_fubon_book(DOC_BOOK_DATA)

        event = normalizer.normalize_bidask(received[0])

        assert self._levels_of(event.bids) == [
            [175_390_000, 2],
            [175_380_000, 4],
            [175_370_000, 3],
            [175_360_000, 10],
            [175_350_000, 10],
        ]
        assert self._levels_of(event.asks) == [
            [175_410_000, 2],
            [175_420_000, 15],
            [175_430_000, 3],
            [175_440_000, 5],
            [175_450_000, 4],
        ]
        assert event.meta.source_ts == DOC_BOOK_TS_NS

    def test_partial_book_reaches_the_normalizer_without_invented_levels(self, normalizer: Any) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(dict(d)))
        rt._on_fubon_book(_book_frame(bids=_levels((17539, 2), (17538, 4)), asks=_levels((17541, 2))))

        event = normalizer.normalize_bidask(received[0])

        assert self._levels_of(event.bids) == [[175_390_000, 2], [175_380_000, 4]]
        assert self._levels_of(event.asks) == [[175_410_000, 2]]

    def test_empty_book_reaches_the_normalizer_as_empty_sides(self, normalizer: Any) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=lambda d: received.append(dict(d)))
        rt._on_fubon_book(_book_frame(bids=[], asks=[]))

        event = normalizer.normalize_bidask(received[0])

        assert self._levels_of(event.bids) == []
        assert self._levels_of(event.asks) == []


# ---------------------------------------------------------------------------
# Realtime init (fubon-neo 2.5.0): explicit Mode + explicit futopt version
# ---------------------------------------------------------------------------


class TestRealtimeInit:
    def test_subscribe_inits_realtime_in_speed_mode_by_default(self, _fubon_sdk_mode: type[_FakeMode]) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["TXFA4"])

        assert sdk.init_realtime_calls == [(_fubon_sdk_mode.Speed, {"futopt": "v1.1"}, None)]

    def test_subscribe_inits_realtime_in_normal_mode_when_env_is_normal(
        self, monkeypatch: pytest.MonkeyPatch, _fubon_sdk_mode: type[_FakeMode]
    ) -> None:
        monkeypatch.setenv(_MODE_ENV, "normal")
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["TXFA4"])

        assert sdk.init_realtime_calls == [(_fubon_sdk_mode.Normal, {"futopt": "v1.1"}, None)]

    def test_subscribe_pins_futopt_stream_version_explicitly(self) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["TXFA4"])

        assert sdk.init_realtime_calls[0][1] == {"futopt": "v1.1"}

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("speed", "Speed"), ("Speed", "Speed"), (" NORMAL ", "Normal"), ("", "Speed"), ("   ", "Speed")],
    )
    def test_realtime_mode_env_ignores_case_and_blank_means_default(
        self, monkeypatch: pytest.MonkeyPatch, _fubon_sdk_mode: type[_FakeMode], raw: str, expected: str
    ) -> None:
        monkeypatch.setenv(_MODE_ENV, raw)
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        rt.subscribe(["TXFA4"])

        assert sdk.init_realtime_calls[0][0] is _fubon_sdk_mode[expected]

    @pytest.mark.parametrize("raw", ["turbo", "fast", "speed,normal", "0", "low-latency"])
    def test_invalid_realtime_mode_env_fails_at_init_before_any_sdk_call(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(_MODE_ENV, raw)
        sdk = _make_sdk()

        with pytest.raises(ValueError, match=_MODE_ENV):
            _make_runtime(sdk)

        assert sdk.init_realtime_calls == []
        assert sdk.futopt_ws.calls == []

    def test_realtime_mode_env_is_read_once_at_init(
        self, monkeypatch: pytest.MonkeyPatch, _fubon_sdk_mode: type[_FakeMode]
    ) -> None:
        monkeypatch.setenv(_MODE_ENV, "normal")
        sdk = _make_sdk()
        rt = _make_runtime(sdk)
        monkeypatch.setenv(_MODE_ENV, "speed")  # changed after init: must not be re-read

        rt.subscribe(["TXFA4"])

        assert sdk.init_realtime_calls[0][0] is _fubon_sdk_mode.Normal

    def test_subscribe_refuses_to_start_when_sdk_mode_enum_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "fubon_neo.sdk", None)  # makes the import raise ImportError
        sdk = _make_sdk()
        rt = _make_runtime(sdk)

        with pytest.raises(ImportError):
            rt.subscribe(["TXFA4"])

        assert sdk.init_realtime_calls == []
        assert rt._running is False
        assert sdk.futopt_ws.calls == []


# ---------------------------------------------------------------------------
# futopt v1.1 trial-matching frames (isTrial) are dropped before normalization
# ---------------------------------------------------------------------------


class TestTrialFrames:
    def test_trade_trial_frame_never_reaches_tick_callback(self, metrics: Any) -> None:
        rt = _make_runtime()
        on_tick = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=MagicMock())

        rt._on_fubon_trade(_trade_frame(isTrial=True))

        assert on_tick.call_count == 0
        assert _dropped(metrics, "trades") == 1.0

    def test_book_trial_frame_never_reaches_bidask_callback(self, metrics: Any) -> None:
        rt = _make_runtime()
        on_bidask = MagicMock()
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=on_bidask)

        rt._on_fubon_book(_book_frame(isTrial=True))

        assert on_bidask.call_count == 0
        assert _dropped(metrics, "books") == 1.0

    @pytest.mark.parametrize("extra", [{}, {"isTrial": False}, {"isTrial": None}])
    def test_normal_trade_frame_reaches_tick_callback_and_is_not_counted(self, metrics: Any, extra: dict) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: received.append(dict(d)), on_bidask=MagicMock())

        rt._on_fubon_trade(_trade_frame(**extra))

        assert received == [DOC_TICK]
        assert _dropped(metrics, "trades") == 0.0

    @pytest.mark.parametrize("extra", [{}, {"isTrial": False}, {"isTrial": None}])
    def test_normal_book_frame_reaches_bidask_callback_and_is_not_counted(self, metrics: Any, extra: dict) -> None:
        rt = _make_runtime()
        received: list[dict[str, Any]] = []
        rt.register_quote_callbacks(
            on_tick=MagicMock(),
            on_bidask=lambda d: received.append({"code": d["code"], "bid_price": list(d["bid_price"])}),
        )

        rt._on_fubon_book(_book_frame(**extra))

        assert received == [{"code": "TXFA4", "bid_price": DOC_BOOK_OUT["bid_price"]}]
        assert _dropped(metrics, "books") == 0.0

    @pytest.mark.parametrize("flag", [True, 1, "true", "TRUE", " True "])
    def test_trial_flag_representations_are_all_dropped(self, metrics: Any, flag: Any) -> None:
        rt = _make_runtime()
        on_tick = MagicMock()
        on_bidask = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=on_bidask)

        rt._on_fubon_trade(_trade_frame(isTrial=flag))
        rt._on_fubon_book(_book_frame(isTrial=flag))

        assert on_tick.call_count == 0
        assert on_bidask.call_count == 0
        assert _dropped(metrics, "trades") == 1.0
        assert _dropped(metrics, "books") == 1.0

    @pytest.mark.parametrize("flag", [False, 0, "false", "", None])
    def test_non_trial_flag_representations_are_forwarded(self, metrics: Any, flag: Any) -> None:
        rt = _make_runtime()
        on_tick = MagicMock()
        on_bidask = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=on_bidask)

        rt._on_fubon_trade(_trade_frame(isTrial=flag))
        rt._on_fubon_book(_book_frame(isTrial=flag))

        assert on_tick.call_count == 1
        assert on_bidask.call_count == 1
        assert _dropped(metrics, "trades") == 0.0
        assert _dropped(metrics, "books") == 0.0

    def test_trial_drops_are_counted_per_channel(self, metrics: Any) -> None:
        rt = _make_runtime()
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=MagicMock())

        rt._on_fubon_trade(_trade_frame(isTrial=True))
        rt._on_fubon_trade(_trade_frame(isTrial=True))
        rt._on_fubon_book(_book_frame(isTrial=True))

        assert _dropped(metrics, "trades") == 2.0
        assert _dropped(metrics, "books") == 1.0

    def test_trial_frame_between_normal_frames_only_normal_frames_are_forwarded(self, metrics: Any) -> None:
        rt = _make_runtime()
        closes: list[float] = []
        rt.register_quote_callbacks(on_tick=lambda d: closes.append(d["close"]), on_bidask=MagicMock())

        rt._on_fubon_trade(_trade_frame(trades=_trade_entry(17540)))
        rt._on_fubon_trade(_trade_frame(trades=_trade_entry(25000), isTrial=True))  # trial price must not leak
        rt._on_fubon_trade(_trade_frame(trades=_trade_entry(17541), isTrial=False))

        assert closes == [17540, 17541]
        assert _dropped(metrics, "trades") == 1.0

    def test_trial_frame_leaves_the_tick_buffer_untouched(self, metrics: Any) -> None:
        rt = _make_runtime()
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=MagicMock())

        rt._on_fubon_trade(_trade_frame(isTrial=True))
        rt._on_fubon_book(_book_frame(isTrial=True))

        assert rt._tick_buffer == {"code": "", "close": 0, "volume": 0, "total_volume": 0, "ts": 0}

    @pytest.mark.parametrize("channel", ["trades", "books"])
    def test_trial_frame_counts_as_liveness_for_the_watchdog(self, metrics: Any, channel: str) -> None:
        rt = _make_runtime()
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=MagicMock())
        assert rt._last_data_ts == 0.0
        before = time.monotonic()

        if channel == "trades":
            rt._on_fubon_trade(_trade_frame(isTrial=True))
        else:
            rt._on_fubon_book(_book_frame(isTrial=True))

        assert rt._last_data_ts >= before

    @pytest.mark.parametrize("channel", ["trades", "books"])
    def test_envelope_level_trial_message_counts_as_liveness_and_is_never_forwarded(
        self, metrics: Any, channel: str
    ) -> None:
        rt, sdk = _subscribed_runtime()
        on_tick = MagicMock()
        on_bidask = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=on_bidask)
        payload = _trade_frame() if channel == "trades" else _book_frame()
        before = time.monotonic()

        sdk.futopt_ws.deliver(_data_msg(channel, payload, isTrial=True))

        assert on_tick.call_count == 0
        assert on_bidask.call_count == 0
        assert _dropped(metrics, channel) == 1.0
        assert rt._last_data_ts >= before

    @pytest.mark.parametrize("channel", ["trades", "books"])
    def test_payload_level_trial_message_counts_as_liveness_and_is_never_forwarded(
        self, metrics: Any, channel: str
    ) -> None:
        rt, sdk = _subscribed_runtime()
        on_tick = MagicMock()
        on_bidask = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=on_bidask)
        payload = _trade_frame(isTrial=True) if channel == "trades" else _book_frame(isTrial=True)
        before = time.monotonic()

        sdk.futopt_ws.deliver(_data_msg(channel, payload))

        assert on_tick.call_count == 0
        assert on_bidask.call_count == 0
        assert _dropped(metrics, channel) == 1.0
        assert rt._last_data_ts >= before

    def test_trial_frame_after_stop_is_dropped_without_being_counted(self, metrics: Any) -> None:
        rt = _make_runtime()
        on_tick = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=MagicMock())
        rt.stop()

        rt._on_fubon_trade(_trade_frame(isTrial=True))

        assert on_tick.call_count == 0
        assert _dropped(metrics, "trades") == 0.0

    def test_trial_frame_is_still_dropped_when_the_counter_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hft_platform.feed_adapter.fubon import quote_runtime

        broken = MagicMock()
        broken.get.side_effect = RuntimeError("registry unavailable")
        monkeypatch.setattr(quote_runtime, "MetricsRegistry", broken)
        rt = _make_runtime()
        on_tick = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=MagicMock())

        rt._on_fubon_trade(_trade_frame(isTrial=True))

        assert broken.get.call_count == 1
        assert on_tick.call_count == 0

    def test_trial_messages_through_the_registered_listener_never_reach_callbacks(self, metrics: Any) -> None:
        """The listener actually handed to the SDK (not just the private methods) carries the filter."""
        rt, sdk = _subscribed_runtime()
        on_tick = MagicMock()
        on_bidask = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=on_bidask)
        (listener,) = sdk.futopt_ws.listeners["message"]

        listener(_data_msg("trades", _trade_frame(isTrial=True)))
        listener(_data_msg("books", _book_frame(isTrial=True)))
        listener(_data_msg("trades", _trade_frame(), isTrial=True))
        listener(_data_msg("books", _book_frame(), isTrial=True))
        assert on_tick.call_count == 0
        assert on_bidask.call_count == 0
        assert _dropped(metrics, "trades") == 2.0
        assert _dropped(metrics, "books") == 2.0

        listener(_data_msg("trades", _trade_frame()))
        listener(_data_msg("books", _book_frame()))
        assert on_tick.call_count == 1
        assert on_bidask.call_count == 1

    def test_registry_creates_each_trial_drop_channel_at_zero(self, metrics: Any) -> None:
        samples = {
            s.labels["channel"]: s.value
            for metric in metrics.fubon_trial_frames_dropped_total.collect()
            for s in metric.samples
            if s.name.endswith("_total")
        }

        assert samples == {"trades": 0.0, "books": 0.0}

    def test_documented_trial_trade_example_is_dropped_counted_and_keeps_the_feed_alive(self, metrics: Any) -> None:
        rt, on_tick, on_bidask = _runtime_with_mocks()
        before = time.monotonic()

        rt._on_ws_message(_data_msg("trades", DOC_TRIAL_TRADE_DATA))

        assert on_tick.call_count == 0
        assert on_bidask.call_count == 0
        assert _dropped(metrics, "trades") == 1.0
        assert rt._last_data_ts >= before
        assert _reasons(rt) == []

    def test_trial_trade_without_a_match_price_is_dropped_as_a_trial_frame(self, metrics: Any) -> None:
        """Documented: no trial match leaves trades[0].price / size at 0; that is not a zero-priced trade."""
        rt, on_tick, _ = _runtime_with_mocks()

        rt._on_fubon_trade({**DOC_TRIAL_TRADE_DATA, "trades": _trade_entry(0, 0)})

        assert on_tick.call_count == 0
        assert _dropped(metrics, "trades") == 1.0
        assert _reasons(rt) == []

    def test_documented_book_with_the_trial_flag_is_dropped_counted_and_keeps_the_feed_alive(
        self, metrics: Any
    ) -> None:
        rt, on_tick, on_bidask = _runtime_with_mocks()
        before = time.monotonic()

        rt._on_ws_message(_data_msg("books", _book_frame(isTrial=True)))

        assert on_tick.call_count == 0
        assert on_bidask.call_count == 0
        assert _dropped(metrics, "books") == 1.0
        assert rt._last_data_ts >= before
        assert _reasons(rt) == []

    def test_trial_frame_with_an_incomplete_body_is_still_counted_as_a_trial_drop(self, metrics: Any) -> None:
        rt, on_tick, on_bidask = _runtime_with_mocks()

        rt._on_fubon_trade({"isTrial": True})
        rt._on_fubon_book({"isTrial": True})

        assert on_tick.call_count == 0
        assert on_bidask.call_count == 0
        assert _dropped(metrics, "trades") == 1.0
        assert _dropped(metrics, "books") == 1.0
        assert _reasons(rt) == []


# ---------------------------------------------------------------------------
# Restart after stop() (FubonSubscriptionManager.resubscribe = stop() then subscribe())
# ---------------------------------------------------------------------------


def _stale_reasons(rt: Any) -> list[str]:
    """Reasons of the stale-generation warnings; other warnings (for example from stop()) carry no reason."""
    reasons = [call.kwargs.get("reason", "") for call in rt.log.warning.call_args_list]
    return [reason for reason in reasons if reason.startswith("stale_generation")]


class TestRestart:
    def test_stop_then_subscribe_delivers_frames_from_the_new_websocket(self, metrics: Any) -> None:
        sdk = _make_sdk(fresh_ws=True)
        rt = _make_runtime(sdk)
        ticks: list[dict[str, Any]] = []
        books: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: ticks.append(dict(d)), on_bidask=lambda d: books.append(dict(d)))
        rt.subscribe(["TXFA4"])
        old_ws = sdk.futopt_ws
        old_ws.deliver(_data_msg("trades", DOC_TRADE_DATA))
        assert ticks == [DOC_TICK]

        rt.stop()
        rt.subscribe(["TXFA4"])
        new_ws = sdk.futopt_ws
        new_ws.deliver(_data_msg("trades", _trade_frame(trades=_trade_entry(17600, 2))))
        new_ws.deliver(_data_msg("books", DOC_BOOK_DATA))

        assert new_ws is not old_ws
        assert [tick["close"] for tick in ticks] == [17540, 17600]
        assert books == [DOC_BOOK_OUT]

    def test_restart_opens_a_fresh_connection_and_subscribes_again(self) -> None:
        sdk = _make_sdk(fresh_ws=True)
        rt = _make_runtime(sdk)
        rt.subscribe(["TXFA4"])
        old_ws = sdk.futopt_ws

        rt.stop()
        rt.subscribe(["TXFA4"])

        new_ws = sdk.futopt_ws
        assert len(sdk.init_realtime_calls) == 2
        assert old_ws.disconnect_calls == 1
        assert new_ws.connect_calls == 1
        assert new_ws.subscribe_calls == [
            {"channel": "trades", "symbol": "TXFA4"},
            {"channel": "books", "symbol": "TXFA4"},
        ]
        assert rt._subscribed == {"TXFA4"}
        assert rt._stopped is False
        assert rt._running is True
        assert rt._ws is new_ws

    def test_restart_on_the_same_websocket_object_publishes_each_frame_once(self, metrics: Any) -> None:
        """The SDK keeps the old listener attached; the new connection's frame must not be published twice."""
        sdk = _make_sdk()
        rt = _make_runtime(sdk)
        ticks: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: ticks.append(dict(d)), on_bidask=MagicMock())
        rt.subscribe(["TXFA4"])
        rt.stop()
        rt.subscribe(["TXFA4"])
        ws = sdk.futopt_ws
        assert len(ws.listeners["message"]) == 2  # old and new closure are both attached

        ws.deliver(_data_msg("trades", DOC_TRADE_DATA))

        assert ticks == [DOC_TICK]

    def test_late_frame_via_the_old_listener_after_restart_is_not_forwarded(self, metrics: Any) -> None:
        sdk = _make_sdk(fresh_ws=True)
        rt = _make_runtime(sdk)
        on_tick = MagicMock()
        on_bidask = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=on_bidask)
        rt.subscribe(["TXFA4"])
        (old_listener,) = sdk.futopt_ws.listeners["message"]
        rt.stop()
        rt.subscribe(["TXFA4"])
        (new_listener,) = sdk.futopt_ws.listeners["message"]

        old_listener(_data_msg("trades", DOC_TRADE_DATA))
        old_listener(_data_msg("books", DOC_BOOK_DATA))

        assert on_tick.call_count == 0
        assert on_bidask.call_count == 0

        new_listener(_data_msg("trades", DOC_TRADE_DATA))
        assert on_tick.call_count == 1

    def test_late_frame_between_stop_and_restart_is_dropped_and_counted(self, metrics: Any) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)
        rt.log = MagicMock()
        on_tick = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=MagicMock())
        rt.subscribe(["TXFA4"])
        (listener,) = sdk.futopt_ws.listeners["message"]
        rt.stop()

        listener(_data_msg("trades", DOC_TRADE_DATA))

        assert on_tick.call_count == 0
        assert _stale_reasons(rt) == ["stale_generation_message"]

    def test_stale_frames_are_counted_in_a_throttled_warning(self, metrics: Any) -> None:
        sdk = _make_sdk(fresh_ws=True)
        rt = _make_runtime(sdk)
        rt.subscribe(["TXFA4"])
        (old_listener,) = sdk.futopt_ws.listeners["message"]
        rt.stop()
        rt.subscribe(["TXFA4"])
        rt.log = MagicMock()

        for _ in range(2001):
            old_listener(_data_msg("trades", DOC_TRADE_DATA))

        counts = [
            call.kwargs["count"]
            for call in rt.log.warning.call_args_list
            if call.kwargs.get("reason") == "stale_generation_message"
        ]
        assert counts == [1, 1000, 2000]

    def test_stale_frame_neither_counts_as_trial_drop_nor_refreshes_liveness(self, metrics: Any) -> None:
        sdk = _make_sdk(fresh_ws=True)
        rt = _make_runtime(sdk)
        rt.register_quote_callbacks(on_tick=MagicMock(), on_bidask=MagicMock())
        rt.subscribe(["TXFA4"])
        (old_listener,) = sdk.futopt_ws.listeners["message"]
        rt.stop()
        rt.subscribe(["TXFA4"])
        assert rt._last_data_ts == 0.0

        old_listener(_data_msg("trades", _trade_frame(isTrial=True)))
        old_listener(_data_msg("trades", DOC_TRADE_DATA))

        assert _dropped(metrics, "trades") == 0.0
        assert rt._last_data_ts == 0.0

    def test_stale_error_listener_after_restart_is_dropped_and_the_new_one_is_logged(self, metrics: Any) -> None:
        sdk = _make_sdk(fresh_ws=True)
        rt = _make_runtime(sdk)
        rt.subscribe(["TXFA4"])
        (old_error_listener,) = sdk.futopt_ws.listeners["error"]
        rt.stop()
        rt.subscribe(["TXFA4"])
        (new_error_listener,) = sdk.futopt_ws.listeners["error"]
        rt.log = MagicMock()

        old_error_listener(ConnectionError("old socket reset"))
        assert rt.log.error.call_count == 0
        assert _stale_reasons(rt) == ["stale_generation_error"]

        new_error_listener(ConnectionError("new socket reset"))
        assert rt.log.error.call_count == 1
        assert rt.log.error.call_args.kwargs["error"] == "new socket reset"

    def test_restart_does_not_reuse_subscription_ids_of_the_old_connection(self, metrics: Any) -> None:
        sdk = _make_sdk(fresh_ws=True)
        rt = _make_runtime(sdk)
        rt.subscribe(["TXFA4"])
        sdk.futopt_ws.ack_all()
        rt.stop()
        rt.subscribe(["TXFA4"])
        new_ws = sdk.futopt_ws
        rt.log = MagicMock()

        rt.unsubscribe(["TXFA4"])  # the new connection has not acknowledged anything yet

        assert new_ws.unsubscribe_calls == []
        assert rt.log.warning.call_count == 2  # one "no subscription id" per channel

    def test_restart_routes_by_the_new_connections_subscription_ids(self, metrics: Any) -> None:
        sdk = _make_sdk(fresh_ws=True)
        rt = _make_runtime(sdk)
        on_tick = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=MagicMock())
        rt.subscribe(["TXFA4"])
        sdk.futopt_ws.ack_all()
        rt.stop()
        rt.subscribe(["TXFA4"])
        new_ws = sdk.futopt_ws

        new_ws.deliver(_envelope("data", id=_sub_id("trades", "TXFA4"), data=DOC_TRADE_DATA))  # id not acked yet
        assert on_tick.call_count == 0

        new_ws.ack_all()
        new_ws.deliver(_envelope("data", id=_sub_id("trades", "TXFA4"), data=DOC_TRADE_DATA))
        assert on_tick.call_count == 1

    def test_failed_reconnect_leaves_the_runtime_stopped_and_old_frames_dropped(self, metrics: Any) -> None:
        sdk = _make_sdk(fresh_ws=True)
        rt = _make_runtime(sdk)
        on_tick = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=MagicMock())
        rt.subscribe(["TXFA4"])
        (old_listener,) = sdk.futopt_ws.listeners["message"]
        rt.stop()
        sdk.next_connect_error = RuntimeError("authentication failed")
        with pytest.raises(RuntimeError, match="authentication failed"):
            rt.subscribe(["TXFA4"])

        assert rt._stopped is True
        assert rt._ws is None
        assert rt._subscribed == set()
        old_listener(_data_msg("trades", DOC_TRADE_DATA))
        assert on_tick.call_count == 0

    def test_retry_after_a_failed_connect_on_a_reused_websocket_publishes_each_frame_once(self, metrics: Any) -> None:
        sdk = _make_sdk()
        sdk.futopt_ws.connect_error = RuntimeError("authentication failed")
        rt = _make_runtime(sdk)
        ticks: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: ticks.append(dict(d)), on_bidask=MagicMock())
        with pytest.raises(RuntimeError):
            rt.subscribe(["TXFA4"])
        sdk.futopt_ws.connect_error = None

        rt.subscribe(["TXFA4"])
        sdk.futopt_ws.deliver(_data_msg("trades", DOC_TRADE_DATA))

        assert ticks == [DOC_TICK]

    def test_resubscribe_cycles_keep_one_live_listener_at_a_time(self, metrics: Any) -> None:
        sdk = _make_sdk()
        rt = _make_runtime(sdk)
        ticks: list[dict[str, Any]] = []
        rt.register_quote_callbacks(on_tick=lambda d: ticks.append(dict(d)), on_bidask=MagicMock())
        rt.subscribe(["TXFA4"])
        for _ in range(3):
            rt.stop()
            rt.subscribe(["TXFA4"])

        sdk.futopt_ws.deliver(_data_msg("trades", DOC_TRADE_DATA))

        assert len(sdk.futopt_ws.listeners["message"]) == 4
        assert ticks == [DOC_TICK]

    def test_stop_after_restart_drops_the_new_connections_frames_too(self, metrics: Any) -> None:
        sdk = _make_sdk(fresh_ws=True)
        rt = _make_runtime(sdk)
        on_tick = MagicMock()
        rt.register_quote_callbacks(on_tick=on_tick, on_bidask=MagicMock())
        rt.subscribe(["TXFA4"])
        rt.stop()
        rt.subscribe(["TXFA4"])
        (new_listener,) = sdk.futopt_ws.listeners["message"]

        rt.stop()
        new_listener(_data_msg("trades", DOC_TRADE_DATA))

        assert on_tick.call_count == 0
        assert rt._stopped is True

    def test_restart_retires_the_old_watchdog_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stop() joins the watchdog with a timeout; a thread still asleep must not outlive a restart."""
        real_join = threading.Thread.join
        sdk = _make_sdk(fresh_ws=True)
        rt = _make_runtime(sdk)
        rt.subscribe(["TXFA4"])
        rt.start_quote_watchdog(timeout_s=0.2)
        old_thread = rt._watchdog_thread
        assert old_thread is not None
        monkeypatch.setattr(old_thread, "join", lambda timeout=None: None)  # simulate the join timing out

        rt.stop()
        assert old_thread.is_alive(), "precondition: the old watchdog is still asleep after stop()"
        rt.subscribe(["TXFA4"])  # restart: _running is True again
        rt.start_quote_watchdog(timeout_s=0.2)
        new_thread = rt._watchdog_thread

        real_join(old_thread, timeout=3.0)
        try:
            assert not old_thread.is_alive()
            assert new_thread is not None
            assert new_thread is not old_thread
            assert new_thread.is_alive()
        finally:
            rt.stop()
            if new_thread is not None:
                real_join(new_thread, timeout=3.0)
