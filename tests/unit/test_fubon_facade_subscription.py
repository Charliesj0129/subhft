"""FubonClientFacade market-data subscription.

The facade hands the operator's configured codes (HFT_SYMBOLS) to the real
FubonSubscriptionManager, with the session flag for the current time. It never
accepts an empty list or an alias code and subscribes to nothing without saying so.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from tests.unit.fubon_mock_helper import install_fubon_neo_mock

install_fubon_neo_mock()

from hft_platform.feed_adapter.fubon import facade as facade_module
from hft_platform.feed_adapter.fubon.facade import FubonClientFacade
from hft_platform.feed_adapter.fubon.subscription_manager import FubonSubscriptionManager

_TAIPEI = ZoneInfo("Asia/Taipei")


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 11, 24, hour, minute, tzinfo=_TAIPEI)


def _cb(*args, **kwargs) -> None:
    return None


@pytest.fixture(autouse=True)
def _no_inherited_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HFT_SYMBOLS", raising=False)


@pytest.fixture(autouse=True)
def _day_session(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin the clock to the day session so no test depends on the time of day.
    monkeypatch.setattr(facade_module, "_session_clock", lambda: _at(10, 0))


@pytest.fixture
def quote_rt(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    rt = MagicMock(name="quote_runtime")
    monkeypatch.setattr(facade_module, "_QuoteRT", lambda sdk: rt)
    return rt


# Facades opened by a test; closed afterwards so no session loop outlives its test.
_OPENED: list[FubonClientFacade] = []


@pytest.fixture(autouse=True)
def _stop_session_loops() -> Iterator[None]:
    yield
    for facade in _OPENED:
        facade.close()
    _OPENED.clear()


def _facade() -> FubonClientFacade:
    facade = FubonClientFacade(symbols_path=None, broker_config={})
    _OPENED.append(facade)
    return facade


def test_subscribe_basket_subscribes_configured_codes(monkeypatch: pytest.MonkeyPatch, quote_rt: MagicMock) -> None:
    monkeypatch.setenv("HFT_SYMBOLS", "TXFL6")
    _facade().subscribe_basket(_cb)
    quote_rt.register_quote_callbacks.assert_called_once_with(_cb, _cb)
    quote_rt.subscribe.assert_called_once_with(["TXFL6"], after_hours=False)
    quote_rt.start_quote_watchdog.assert_called_once()


def test_subscribe_basket_uses_real_subscription_manager(monkeypatch: pytest.MonkeyPatch, quote_rt: MagicMock) -> None:
    monkeypatch.setenv("HFT_SYMBOLS", "TXFL6")
    facade = _facade()
    facade.subscribe_basket(_cb)
    assert isinstance(facade._subscription_manager, FubonSubscriptionManager)
    assert facade._subscription_manager.subscribed_codes == {"TXFL6"}


def test_subscribe_basket_refuses_empty_symbol_list(quote_rt: MagicMock) -> None:
    with pytest.raises(RuntimeError, match="HFT_SYMBOLS"):
        _facade().subscribe_basket(_cb)
    quote_rt.subscribe.assert_not_called()


def test_subscribe_basket_rejects_alias_codes(monkeypatch: pytest.MonkeyPatch, quote_rt: MagicMock) -> None:
    monkeypatch.setenv("HFT_SYMBOLS", "TMFR1")
    with pytest.raises(RuntimeError, match="alias"):
        _facade().subscribe_basket(_cb)
    quote_rt.subscribe.assert_not_called()


def test_subscribe_basket_requests_night_flag_at_night(monkeypatch: pytest.MonkeyPatch, quote_rt: MagicMock) -> None:
    monkeypatch.setenv("HFT_SYMBOLS", "TXFL6")
    monkeypatch.setattr(facade_module, "_session_clock", lambda: _at(16, 0))
    _facade().subscribe_basket(_cb)
    quote_rt.subscribe.assert_called_once_with(["TXFL6"], after_hours=True)


def test_resubscribe_restarts_configured_codes(monkeypatch: pytest.MonkeyPatch, quote_rt: MagicMock) -> None:
    monkeypatch.setenv("HFT_SYMBOLS", "TXFL6,TMFL6")
    facade = _facade()
    facade.subscribe_basket(_cb)
    quote_rt.reset_mock()
    assert facade.resubscribe() is True
    quote_rt.stop.assert_called_once()
    quote_rt.subscribe.assert_called_once_with(["TXFL6", "TMFL6"], after_hours=False)


def test_resubscribe_requests_night_flag_after_day_close(monkeypatch: pytest.MonkeyPatch, quote_rt: MagicMock) -> None:
    monkeypatch.setenv("HFT_SYMBOLS", "TXFL6")
    facade = _facade()
    facade.subscribe_basket(_cb)
    quote_rt.reset_mock()
    monkeypatch.setattr(facade_module, "_session_clock", lambda: _at(16, 0))
    assert facade.resubscribe() is True
    quote_rt.subscribe.assert_called_once_with(["TXFL6"], after_hours=True)


def test_resubscribe_before_subscribe_basket_returns_false(quote_rt: MagicMock) -> None:
    assert _facade().resubscribe() is False
    quote_rt.subscribe.assert_not_called()


def _session_relogin_ok(facade: FubonClientFacade, *, logged_in: bool = False) -> MagicMock:
    # Swap the SDK-backed session and gateways for mocks, so reconnect() runs without a broker.
    session = MagicMock(name="session_runtime")
    session.is_logged_in = logged_in
    session.reconnect.return_value = True
    facade._session_runtime = session
    facade._order_gateway = MagicMock(name="order_gateway")
    facade._account_gateway = MagicMock(name="account_gateway")
    return session


def test_reconnect_resubscribes_basket_after_relogin(monkeypatch: pytest.MonkeyPatch, quote_rt: MagicMock) -> None:
    monkeypatch.setenv("HFT_SYMBOLS", "TXFL6")
    facade = _facade()
    facade.subscribe_basket(_cb)
    session = _session_relogin_ok(facade)
    quote_rt.reset_mock()
    assert facade.reconnect("heartbeat_gap 70.0s") is True
    session.reconnect.assert_called_once()
    quote_rt.subscribe.assert_called_once_with(["TXFL6"], after_hours=False)


def test_reconnect_resubscribes_inside_resubscribe_cooldown(
    monkeypatch: pytest.MonkeyPatch, quote_rt: MagicMock
) -> None:
    monkeypatch.setenv("HFT_SYMBOLS", "TXFL6")
    facade = _facade()
    facade.subscribe_basket(_cb)
    _session_relogin_ok(facade)
    assert facade.resubscribe() is True  # the monitor's own resubscribe just ran
    quote_rt.reset_mock()
    assert facade.reconnect("heartbeat_gap 70.0s") is True
    quote_rt.subscribe.assert_called_once_with(["TXFL6"], after_hours=False)


def test_reconnect_reports_false_when_manager_closed(monkeypatch: pytest.MonkeyPatch, quote_rt: MagicMock) -> None:
    monkeypatch.setenv("HFT_SYMBOLS", "TXFL6")
    facade = _facade()
    facade.subscribe_basket(_cb)
    _session_relogin_ok(facade)
    facade.close()
    quote_rt.reset_mock()
    assert facade.reconnect("heartbeat_gap 70.0s") is False
    quote_rt.subscribe.assert_not_called()


def test_reconnect_before_subscribe_basket_keeps_live_session(quote_rt: MagicMock) -> None:
    facade = _facade()
    session = _session_relogin_ok(facade, logged_in=True)
    assert facade.reconnect("heartbeat_gap 70.0s") is False
    session.reconnect.assert_not_called()
    quote_rt.subscribe.assert_not_called()


def test_reconnect_before_subscribe_basket_relogs_in_but_reports_false(quote_rt: MagicMock) -> None:
    facade = _facade()
    session = _session_relogin_ok(facade, logged_in=False)
    assert facade.reconnect("heartbeat_gap 70.0s") is False
    session.reconnect.assert_called_once()
    quote_rt.subscribe.assert_not_called()


def test_forced_reconnect_before_subscribe_basket_keeps_live_session(quote_rt: MagicMock) -> None:
    facade = _facade()
    session = _session_relogin_ok(facade, logged_in=True)
    assert facade.reconnect("session_rollover", force=True) is False
    session.reconnect.assert_not_called()
    quote_rt.subscribe.assert_not_called()
