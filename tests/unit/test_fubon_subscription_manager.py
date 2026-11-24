"""Tests for FubonSubscriptionManager."""

from __future__ import annotations

import enum
import json
import sys
import time
import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.fubon.quote_runtime import FubonQuoteRuntime
from hft_platform.feed_adapter.fubon.subscription_manager import (
    _RESUBSCRIBE_COOLDOWN_S,
    FubonSubscriptionManager,
)


@pytest.fixture()
def mock_quote_runtime() -> MagicMock:
    """Return a mock FubonQuoteRuntime."""
    rt = MagicMock()
    rt.register_quote_callbacks = MagicMock()
    rt.subscribe = MagicMock()
    rt.start_quote_watchdog = MagicMock()
    rt.stop = MagicMock()
    return rt


# ------------------------------------------------------------------ #
# subscribe_basket
# ------------------------------------------------------------------ #


class TestSubscribeBasket:
    def test_registers_callbacks_and_subscribes(self, mock_quote_runtime: MagicMock) -> None:
        symbols = ["2330", "2317", "2454"]
        mgr = FubonSubscriptionManager(mock_quote_runtime, symbols)
        cb = MagicMock()

        mgr.subscribe_basket(cb)

        mock_quote_runtime.register_quote_callbacks.assert_called_once_with(cb, cb)
        mock_quote_runtime.subscribe.assert_called_once_with(["2330", "2317", "2454"], after_hours=False)
        assert mgr.subscribed_codes == {"2330", "2317", "2454"}

    def test_starts_watchdog(self, mock_quote_runtime: MagicMock) -> None:
        mgr = FubonSubscriptionManager(mock_quote_runtime, ["2330"])
        mgr.subscribe_basket(MagicMock())

        mock_quote_runtime.start_quote_watchdog.assert_called_once()

    def test_handles_dict_symbols(self, mock_quote_runtime: MagicMock) -> None:
        symbols: list[dict] = [
            {"code": "2330", "exchange": "TSE"},
            {"code": "2317", "exchange": "TSE"},
        ]
        mgr = FubonSubscriptionManager(mock_quote_runtime, symbols)
        mgr.subscribe_basket(MagicMock())

        mock_quote_runtime.subscribe.assert_called_once_with(["2330", "2317"], after_hours=False)

    def test_max_subscriptions_cap(self, mock_quote_runtime: MagicMock) -> None:
        symbols = [f"SYM{i}" for i in range(250)]
        mgr = FubonSubscriptionManager(mock_quote_runtime, symbols, max_subscriptions=200)
        mgr.subscribe_basket(MagicMock())

        subscribed = mock_quote_runtime.subscribe.call_args[0][0]
        assert len(subscribed) == 200


# ------------------------------------------------------------------ #
# resubscribe
# ------------------------------------------------------------------ #


class TestResubscribe:
    def test_resubscribe_success(self, mock_quote_runtime: MagicMock) -> None:
        mgr = FubonSubscriptionManager(mock_quote_runtime, ["2330", "2317"])
        result = mgr.resubscribe()

        assert result is True
        mock_quote_runtime.stop.assert_called_once()
        mock_quote_runtime.subscribe.assert_called_once_with(["2330", "2317"], after_hours=False)
        mock_quote_runtime.start_quote_watchdog.assert_called_once()

    def test_resubscribe_cooldown_rejects(self, mock_quote_runtime: MagicMock) -> None:
        mgr = FubonSubscriptionManager(mock_quote_runtime, ["2330"])

        first = mgr.resubscribe()
        assert first is True

        # Immediate second call should be rejected
        second = mgr.resubscribe()
        assert second is False

        # stop() should have been called only once (first call)
        assert mock_quote_runtime.stop.call_count == 1

    def test_resubscribe_succeeds_after_cooldown(
        self, mock_quote_runtime: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mgr = FubonSubscriptionManager(mock_quote_runtime, ["2330"])

        # First call succeeds
        first = mgr.resubscribe()
        assert first is True

        # Simulate cooldown elapsed by backdating _last_resubscribe_ts
        mgr._last_resubscribe_ts = time.monotonic() - _RESUBSCRIBE_COOLDOWN_S - 1.0

        second = mgr.resubscribe()
        assert second is True
        assert mock_quote_runtime.stop.call_count == 2

    def test_resubscribe_no_symbols(self, mock_quote_runtime: MagicMock) -> None:
        mgr = FubonSubscriptionManager(mock_quote_runtime, [])
        result = mgr.resubscribe()

        assert result is False
        mock_quote_runtime.stop.assert_not_called()


# ------------------------------------------------------------------ #
# set_execution_callbacks
# ------------------------------------------------------------------ #


class TestSetExecutionCallbacks:
    def test_stores_callbacks(self, mock_quote_runtime: MagicMock) -> None:
        mgr = FubonSubscriptionManager(mock_quote_runtime, ["2330"])
        on_order = MagicMock()
        on_deal = MagicMock()

        mgr.set_execution_callbacks(on_order, on_deal)

        assert mgr._on_order_cb is on_order
        assert mgr._on_deal_cb is on_deal


# ------------------------------------------------------------------ #
# _extract_codes
# ------------------------------------------------------------------ #


class TestExtractCodes:
    def test_list_of_strings(self) -> None:
        codes = FubonSubscriptionManager._extract_codes(["2330", "2317"])
        assert codes == ["2330", "2317"]

    def test_list_of_dicts(self) -> None:
        symbols: list[dict] = [
            {"code": "2330", "exchange": "TSE"},
            {"code": "2317", "exchange": "OTC"},
        ]
        codes = FubonSubscriptionManager._extract_codes(symbols)
        assert codes == ["2330", "2317"]

    def test_dict_without_code_key_skipped(self) -> None:
        symbols: list[dict] = [
            {"code": "2330"},
            {"name": "no_code"},
        ]
        codes = FubonSubscriptionManager._extract_codes(symbols)
        assert codes == ["2330"]

    def test_empty_list(self) -> None:
        codes = FubonSubscriptionManager._extract_codes([])
        assert codes == []

    def test_mixed_types(self) -> None:
        # Even though type hint says list[str] | list[dict], handle mixed gracefully
        symbols: list = ["2330", {"code": "2317"}]
        codes = FubonSubscriptionManager._extract_codes(symbols)
        assert codes == ["2330", "2317"]


# ------------------------------------------------------------------ #
# resubscribe end to end: real FubonQuoteRuntime on a fake SDK
# ------------------------------------------------------------------ #
#
# resubscribe() is stop() then subscribe() on the SAME runtime.  These tests drive that path
# with a fake of the fubon-neo 2.5.0 futopt websocket client and documented trades frames, so a
# resubscribe that leaves the feed dead (or publishes a frame twice) shows up as a tick count.

_MODE_ENV = "HFT_FUBON_REALTIME_MODE"
_TRADE_DATA: dict[str, Any] = {
    "symbol": "TXFA4",
    "type": "FUTURE",
    "exchange": "TAIFEX",
    "trades": [{"price": 17540, "size": 1, "bid": 17539, "ask": 17540}],
    "total": {"tradeVolume": 12174, "totalBidMatch": 8760, "totalAskMatch": 7907},
    "time": 1702956487023000,
    "serial": 159250,
}


class _FakeMode(enum.Enum):
    Speed = "speed"
    Normal = "normal"


class _FakeWs:
    """Stand-in for sdk.marketdata.websocket_client.futopt: on / connect / subscribe / unsubscribe / disconnect."""

    def __init__(self) -> None:
        self.listeners: dict[str, list[Any]] = {}
        self.subscribe_calls: list[dict[str, Any]] = []
        self.unsubscribe_calls: list[dict[str, Any]] = []
        self.connect_calls = 0
        self.disconnect_calls = 0

    def on(self, event: str, listener: Any) -> None:
        self.listeners.setdefault(event, []).append(listener)

    def connect(self) -> None:
        self.connect_calls += 1

    def subscribe(self, params: dict[str, Any]) -> None:
        self.subscribe_calls.append(params)

    def unsubscribe(self, params: dict[str, Any]) -> None:
        self.unsubscribe_calls.append(params)

    def disconnect(self) -> None:
        self.disconnect_calls += 1

    def deliver(self, raw: str) -> None:
        for listener in list(self.listeners.get("message", [])):
            listener(raw)

    def ack_all(self) -> None:
        """Answer every recorded subscribe with the service's subscribed event (carries the id)."""
        for params in list(self.subscribe_calls):
            data = {"id": f"id-{params['channel']}-{params['symbol']}", **params}
            self.deliver(json.dumps({"event": "subscribed", "data": data}))


class _FakeSdk:
    """marketdata exists only after init_realtime(); fresh_ws builds a new client per reconnect."""

    def __init__(self, *, fresh_ws: bool) -> None:
        self.fresh_ws = fresh_ws
        self.all_ws: list[_FakeWs] = []
        self.init_realtime_calls = 0

    @property
    def current_ws(self) -> _FakeWs:
        return self.all_ws[-1]

    def init_realtime(self, mode: Any = None, version: Any = None, base_url: Any = None) -> None:
        self.init_realtime_calls += 1
        if self.fresh_ws or not self.all_ws:
            self.all_ws.append(_FakeWs())
        self.marketdata = SimpleNamespace(websocket_client=SimpleNamespace(futopt=self.current_ws))


def _trade_msg(price: int) -> str:
    data = {**_TRADE_DATA, "trades": [{"price": price, "size": 1}]}
    return json.dumps({"event": "data", "data": data, "id": "<CHANNEL_ID>", "channel": "trades"})


def _tick(price: int) -> dict[str, Any]:
    return {"code": "TXFA4", "close": price, "volume": 1, "total_volume": 12174, "ts": 1_702_956_487_023_000_000}


def _after_cooldown(mgr: FubonSubscriptionManager) -> None:
    mgr._last_resubscribe_ts = time.monotonic() - _RESUBSCRIBE_COOLDOWN_S - 1.0


@pytest.fixture()
def watchdog_starts(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The SDK stub, a clean env, and a recorder in place of the watchdog thread (its stop() join waits 5 s)."""
    monkeypatch.delenv(_MODE_ENV, raising=False)
    sdk_module = types.ModuleType("fubon_neo.sdk")
    sdk_module.Mode = _FakeMode
    monkeypatch.setitem(sys.modules, "fubon_neo.sdk", sdk_module)
    starts: list[float] = []
    monkeypatch.setattr(
        FubonQuoteRuntime, "start_quote_watchdog", lambda self, timeout_s=30.0: starts.append(timeout_s)
    )
    return starts


@pytest.mark.usefixtures("watchdog_starts")
class TestResubscribeEndToEnd:
    def test_resubscribe_delivers_ticks_from_the_new_websocket(self) -> None:
        sdk = _FakeSdk(fresh_ws=True)
        mgr = FubonSubscriptionManager(FubonQuoteRuntime(sdk), ["TXFA4"])
        ticks: list[dict[str, Any]] = []
        mgr.subscribe_basket(lambda d: ticks.append(dict(d)))
        first_ws = sdk.current_ws
        first_ws.deliver(_trade_msg(17540))
        assert ticks == [_tick(17540)]

        assert mgr.resubscribe() is True
        second_ws = sdk.current_ws
        second_ws.deliver(_trade_msg(17600))

        assert second_ws is not first_ws
        assert ticks == [_tick(17540), _tick(17600)]

    def test_resubscribe_does_not_publish_frames_of_the_old_connection(self) -> None:
        sdk = _FakeSdk(fresh_ws=True)
        mgr = FubonSubscriptionManager(FubonQuoteRuntime(sdk), ["TXFA4"])
        ticks: list[dict[str, Any]] = []
        mgr.subscribe_basket(lambda d: ticks.append(dict(d)))
        first_ws = sdk.current_ws
        assert mgr.resubscribe() is True

        first_ws.deliver(_trade_msg(17999))  # late frame of the retired connection

        assert ticks == []

    def test_resubscribe_publishes_each_frame_once_when_the_sdk_reuses_the_websocket_object(self) -> None:
        sdk = _FakeSdk(fresh_ws=False)
        mgr = FubonSubscriptionManager(FubonQuoteRuntime(sdk), ["TXFA4"])
        ticks: list[dict[str, Any]] = []
        mgr.subscribe_basket(lambda d: ticks.append(dict(d)))
        assert mgr.resubscribe() is True
        ws = sdk.current_ws
        assert len(ws.listeners["message"]) == 2  # the retired listener is still attached

        ws.deliver(_trade_msg(17540))

        assert ticks == [_tick(17540)]

    def test_resubscribe_moves_every_code_to_the_new_connection(self) -> None:
        sdk = _FakeSdk(fresh_ws=True)
        mgr = FubonSubscriptionManager(FubonQuoteRuntime(sdk), ["TXFA4", "TXFB4"])
        mgr.subscribe_basket(MagicMock())
        first_ws = sdk.current_ws
        first_ws.ack_all()

        assert mgr.resubscribe() is True

        second_ws = sdk.current_ws
        assert len(first_ws.unsubscribe_calls) == 4  # trades + books for each code, by id
        assert first_ws.disconnect_calls == 1
        assert second_ws.connect_calls == 1
        assert second_ws.subscribe_calls == [
            {"channel": "trades", "symbol": "TXFA4"},
            {"channel": "books", "symbol": "TXFA4"},
            {"channel": "trades", "symbol": "TXFB4"},
            {"channel": "books", "symbol": "TXFB4"},
        ]
        assert mgr.subscribed_codes == {"TXFA4", "TXFB4"}

    def test_each_resubscribe_after_the_cooldown_keeps_delivering_exactly_once(self) -> None:
        sdk = _FakeSdk(fresh_ws=False)
        mgr = FubonSubscriptionManager(FubonQuoteRuntime(sdk), ["TXFA4"])
        ticks: list[dict[str, Any]] = []
        mgr.subscribe_basket(lambda d: ticks.append(dict(d)))
        for _ in range(3):
            _after_cooldown(mgr)
            assert mgr.resubscribe() is True

        sdk.current_ws.deliver(_trade_msg(17540))

        assert sdk.init_realtime_calls == 4
        assert ticks == [_tick(17540)]

    def test_resubscribe_restarts_the_watchdog(self, watchdog_starts: list[float]) -> None:
        sdk = _FakeSdk(fresh_ws=True)
        mgr = FubonSubscriptionManager(FubonQuoteRuntime(sdk), ["TXFA4"])
        mgr.subscribe_basket(MagicMock())
        assert len(watchdog_starts) == 1

        assert mgr.resubscribe() is True

        assert len(watchdog_starts) == 2

    def test_resubscribe_inside_the_cooldown_leaves_the_connection_untouched(self) -> None:
        sdk = _FakeSdk(fresh_ws=True)
        mgr = FubonSubscriptionManager(FubonQuoteRuntime(sdk), ["TXFA4"])
        ticks: list[dict[str, Any]] = []
        mgr.subscribe_basket(lambda d: ticks.append(dict(d)))
        assert mgr.resubscribe() is True

        assert mgr.resubscribe() is False  # cooldown: no stop(), no new connection

        sdk.current_ws.deliver(_trade_msg(17540))
        assert sdk.init_realtime_calls == 2
        assert ticks == [_tick(17540)]
