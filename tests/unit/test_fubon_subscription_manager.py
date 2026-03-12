"""Tests for FubonSubscriptionManager."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

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
        mock_quote_runtime.subscribe.assert_called_once_with(["2330", "2317", "2454"])
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

        mock_quote_runtime.subscribe.assert_called_once_with(["2330", "2317"])

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
        mock_quote_runtime.subscribe.assert_called_once_with(["2330", "2317"])
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
