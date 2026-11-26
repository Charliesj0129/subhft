"""The futopt WebSocket connection cap: the service allows 7 concurrent connections.

Every runtime that opens a socket claims a process-wide slot before it connects, and stop()
gives it back. A relogin or resubscribe stops first, so it reuses the slot it released.
"""

from __future__ import annotations

import enum
import sys
import types
from unittest.mock import MagicMock

import pytest

from tests.unit.fubon_mock_helper import install_fubon_neo_mock

install_fubon_neo_mock()

from hft_platform.feed_adapter.fubon import quote_runtime as qr


class _FakeMode(enum.Enum):
    Speed = "Speed"
    Normal = "Normal"


@pytest.fixture(autouse=True)
def _fubon_sdk_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mock SDK has no ``Mode``; connect() resolves it from ``fubon_neo.sdk``."""
    sdk_module = types.ModuleType("fubon_neo.sdk")
    sdk_module.Mode = _FakeMode
    monkeypatch.setitem(sys.modules, "fubon_neo.sdk", sdk_module)


def _runtime() -> qr.FubonQuoteRuntime:
    return qr.FubonQuoteRuntime(MagicMock(name="sdk"))


def test_eighth_subscribe_is_refused_while_seven_connections_are_open() -> None:
    held = [_runtime() for _ in range(qr.MAX_LIVE_CONNECTIONS)]
    for rt in held:
        rt.subscribe(["TXFA4"])
    extra_sdk = MagicMock(name="sdk")
    extra = qr.FubonQuoteRuntime(extra_sdk)
    with pytest.raises(qr.FubonConnectionLimitError):
        extra.subscribe(["TXFA4"])
    extra_sdk.init_realtime.assert_not_called()


def test_stop_frees_a_slot_for_the_next_connection() -> None:
    held = [_runtime() for _ in range(qr.MAX_LIVE_CONNECTIONS)]
    for rt in held:
        rt.subscribe(["TXFA4"])
    held[0].stop()
    extra = _runtime()
    extra.subscribe(["TXFA4"])
    assert extra._ws is not None


def test_resubscribe_after_stop_reuses_the_slot_it_released() -> None:
    held = [_runtime() for _ in range(qr.MAX_LIVE_CONNECTIONS)]
    for rt in held:
        rt.subscribe(["TXFA4"])
    held[0].stop()
    held[0].subscribe(["TXFA4"])
    with pytest.raises(qr.FubonConnectionLimitError):
        _runtime().subscribe(["TXFA4"])


def test_failed_connect_gives_its_slot_back_and_closes_the_socket() -> None:
    sdk = MagicMock(name="sdk")
    ws = sdk.marketdata.websocket_client.futopt
    ws.connect.side_effect = RuntimeError("connect refused")
    with pytest.raises(RuntimeError, match="connect refused"):
        qr.FubonQuoteRuntime(sdk).subscribe(["TXFA4"])
    ws.disconnect.assert_called_once()
    assert qr._live_connections == 0
    others = [_runtime() for _ in range(qr.MAX_LIVE_CONNECTIONS)]
    for rt in others:
        rt.subscribe(["TXFA4"])


def test_relogin_stops_the_previous_quote_runtime_and_gives_its_slot_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.feed_adapter.fubon import client as client_module

    monkeypatch.setattr(client_module, "FubonSDK", MagicMock(name="FubonSDK"))
    monkeypatch.setattr(client_module, "build_fubon_sdk", lambda _sdk_cls: (MagicMock(name="sdk"), ""))
    session = MagicMock(name="session")
    session.login.return_value = True
    monkeypatch.setattr(client_module, "FubonSessionRuntime", MagicMock(return_value=session))
    fub = client_module.FubonClient()
    assert fub.login()
    fub._quote_runtime.subscribe(["TXFA4"])
    assert qr._live_connections == 1
    assert fub.login()
    assert qr._live_connections == 0
