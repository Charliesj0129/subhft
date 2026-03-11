"""Unit tests for FubonSubscriptionManager.

Tests cover:
- subscribe_basket: callback registration, subscribe, watchdog start
- resubscribe: cooldown enforcement, stop/restart cycle
- set_execution_callbacks: stores callbacks, wires SDK hooks
- _subscribe_symbol / _unsubscribe_symbol: per-symbol lifecycle
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sdk() -> MagicMock:
    """Return a mock Fubon SDK."""
    sdk = MagicMock()
    # Remove hooks by default so tests can add them selectively
    del sdk.set_on_order_changed
    del sdk.set_on_filled
    return sdk


def _make_quote_runtime() -> MagicMock:
    """Return a mock FubonQuoteRuntime with expected API surface."""
    qr = MagicMock()
    qr.register_quote_callbacks = MagicMock()
    qr.subscribe = MagicMock()
    qr.unsubscribe = MagicMock()
    qr.start_quote_watchdog = MagicMock()
    qr.stop = MagicMock()
    return qr


def _make_symbols() -> list[dict[str, Any]]:
    return [
        {"code": "2330", "exchange": "TWSE", "name": "TSMC"},
        {"code": "2317", "exchange": "TWSE", "name": "Foxconn"},
    ]


def _make_manager(
    sdk: MagicMock | None = None,
    qr: MagicMock | None = None,
    symbols: list[dict[str, Any]] | None = None,
):
    from hft_platform.feed_adapter.fubon.subscription_manager import (
        FubonSubscriptionManager,
    )

    return FubonSubscriptionManager(
        sdk=sdk or _make_sdk(),
        quote_runtime=qr or _make_quote_runtime(),
        symbols=symbols if symbols is not None else _make_symbols(),
    )


# ---------------------------------------------------------------------------
# subscribe_basket
# ---------------------------------------------------------------------------


class TestSubscribeBasket:
    def test_registers_callbacks_and_subscribes(self) -> None:
        qr = _make_quote_runtime()
        cb = MagicMock()
        mgr = _make_manager(qr=qr)

        mgr.subscribe_basket(cb)

        qr.register_quote_callbacks.assert_called_once_with(
            on_tick=cb,
            on_bidask=cb,
        )
        qr.subscribe.assert_called_once_with(["2330", "2317"])
        qr.start_quote_watchdog.assert_called_once()

    def test_stores_tick_callback(self) -> None:
        cb = MagicMock()
        mgr = _make_manager()

        mgr.subscribe_basket(cb)

        assert mgr._tick_callback is cb

    def test_populates_subscribed_codes(self) -> None:
        mgr = _make_manager()
        mgr.subscribe_basket(MagicMock())

        assert mgr._subscribed_codes == {"2330", "2317"}

    def test_skips_symbols_without_code(self) -> None:
        qr = _make_quote_runtime()
        symbols = [{"code": "2330"}, {"name": "no-code"}]
        mgr = _make_manager(qr=qr, symbols=symbols)

        mgr.subscribe_basket(MagicMock())

        qr.subscribe.assert_called_once_with(["2330"])
        assert mgr._subscribed_codes == {"2330"}

    def test_empty_symbols_list(self) -> None:
        qr = _make_quote_runtime()
        mgr = _make_manager(qr=qr, symbols=[])

        mgr.subscribe_basket(MagicMock())

        qr.subscribe.assert_called_once_with([])
        assert mgr._subscribed_codes == set()


# ---------------------------------------------------------------------------
# resubscribe
# ---------------------------------------------------------------------------


class TestResubscribe:
    def test_resubscribe_success(self) -> None:
        qr = _make_quote_runtime()
        mgr = _make_manager(qr=qr)
        mgr._tick_callback = MagicMock()

        result = mgr.resubscribe()

        assert result is True
        qr.stop.assert_called_once()
        qr.register_quote_callbacks.assert_called_once()
        qr.subscribe.assert_called_once_with(["2330", "2317"])
        qr.start_quote_watchdog.assert_called_once()

    def test_resubscribe_cooldown(self) -> None:
        """Second resubscribe within 10s should be skipped."""
        mgr = _make_manager()
        mgr._tick_callback = MagicMock()

        # First resubscribe succeeds
        result1 = mgr.resubscribe()
        assert result1 is True

        # Second immediately after — should be blocked by cooldown
        result2 = mgr.resubscribe()
        assert result2 is False

    def test_resubscribe_after_cooldown(self) -> None:
        """Resubscribe should succeed after cooldown expires."""
        qr = _make_quote_runtime()
        mgr = _make_manager(qr=qr)
        mgr._tick_callback = MagicMock()

        # First resubscribe
        mgr.resubscribe()

        # Simulate cooldown expiry by backdating timestamp
        mgr._last_resubscribe_ts -= 15.0

        result = mgr.resubscribe()
        assert result is True
        assert qr.stop.call_count == 2

    def test_resubscribe_without_tick_callback(self) -> None:
        """Resubscribe without prior subscribe_basket should still work."""
        qr = _make_quote_runtime()
        mgr = _make_manager(qr=qr)

        result = mgr.resubscribe()

        assert result is True
        qr.stop.assert_called_once()
        # No register_quote_callbacks when tick_callback is None
        qr.register_quote_callbacks.assert_not_called()

    def test_resubscribe_error_returns_false(self) -> None:
        qr = _make_quote_runtime()
        qr.stop.side_effect = RuntimeError("connection lost")
        mgr = _make_manager(qr=qr)

        result = mgr.resubscribe()

        assert result is False


# ---------------------------------------------------------------------------
# set_execution_callbacks
# ---------------------------------------------------------------------------


class TestSetExecutionCallbacks:
    def test_stores_callbacks(self) -> None:
        mgr = _make_manager()
        on_order = MagicMock()
        on_deal = MagicMock()

        mgr.set_execution_callbacks(on_order, on_deal)

        assert mgr._on_order_cb is on_order
        assert mgr._on_deal_cb is on_deal

    def test_wires_sdk_hooks_when_available(self) -> None:
        sdk = MagicMock()
        # These hooks exist on the mock by default
        mgr = _make_manager(sdk=sdk)
        on_order = MagicMock()
        on_deal = MagicMock()

        mgr.set_execution_callbacks(on_order, on_deal)

        sdk.set_on_order_changed.assert_called_once_with(on_order)
        sdk.set_on_filled.assert_called_once_with(on_deal)

    def test_no_crash_when_sdk_lacks_hooks(self) -> None:
        sdk = _make_sdk()  # hooks deleted
        mgr = _make_manager(sdk=sdk)

        # Should not raise
        mgr.set_execution_callbacks(MagicMock(), MagicMock())

    def test_sdk_hook_error_does_not_propagate(self) -> None:
        sdk = MagicMock()
        sdk.set_on_order_changed.side_effect = RuntimeError("boom")
        sdk.set_on_filled.side_effect = RuntimeError("boom")
        mgr = _make_manager(sdk=sdk)

        # Should not raise
        mgr.set_execution_callbacks(MagicMock(), MagicMock())

    def test_sdk_none_does_not_crash(self) -> None:
        from hft_platform.feed_adapter.fubon.subscription_manager import (
            FubonSubscriptionManager,
        )

        mgr = FubonSubscriptionManager(
            sdk=None,
            quote_runtime=_make_quote_runtime(),
            symbols=[],
        )
        mgr.set_execution_callbacks(MagicMock(), MagicMock())


# ---------------------------------------------------------------------------
# _subscribe_symbol / _unsubscribe_symbol
# ---------------------------------------------------------------------------


class TestPerSymbol:
    def test_subscribe_symbol_success(self) -> None:
        qr = _make_quote_runtime()
        mgr = _make_manager(qr=qr)

        result = mgr._subscribe_symbol({"code": "2330"}, MagicMock())

        assert result is True
        qr.subscribe.assert_called_once_with(["2330"])
        assert "2330" in mgr._subscribed_codes

    def test_subscribe_symbol_missing_code(self) -> None:
        mgr = _make_manager()

        result = mgr._subscribe_symbol({"name": "no-code"}, MagicMock())

        assert result is False

    def test_subscribe_symbol_error(self) -> None:
        qr = _make_quote_runtime()
        qr.subscribe.side_effect = RuntimeError("rate limited")
        mgr = _make_manager(qr=qr)

        result = mgr._subscribe_symbol({"code": "2330"}, MagicMock())

        assert result is False

    def test_unsubscribe_symbol(self) -> None:
        qr = _make_quote_runtime()
        mgr = _make_manager(qr=qr)
        mgr._subscribed_codes.add("2330")

        mgr._unsubscribe_symbol({"code": "2330"})

        qr.unsubscribe.assert_called_once_with(["2330"])
        assert "2330" not in mgr._subscribed_codes

    def test_unsubscribe_symbol_missing_code(self) -> None:
        qr = _make_quote_runtime()
        mgr = _make_manager(qr=qr)

        mgr._unsubscribe_symbol({"name": "no-code"})

        qr.unsubscribe.assert_not_called()

    def test_unsubscribe_symbol_error_logged(self) -> None:
        qr = _make_quote_runtime()
        qr.unsubscribe.side_effect = RuntimeError("boom")
        mgr = _make_manager(qr=qr)
        mgr._subscribed_codes.add("2330")

        # Should not raise
        mgr._unsubscribe_symbol({"code": "2330"})


# ---------------------------------------------------------------------------
# __slots__ verification
# ---------------------------------------------------------------------------


class TestSlots:
    def test_has_slots(self) -> None:
        from hft_platform.feed_adapter.fubon.subscription_manager import (
            FubonSubscriptionManager,
        )

        assert hasattr(FubonSubscriptionManager, "__slots__")
        assert "__dict__" not in dir(_make_manager())
