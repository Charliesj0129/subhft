"""The warm-up probe must establish a session without touching the order book.

``OrderGateway.warm_session`` runs ahead of a market open, on a connection
whose state is by definition unknown. Two properties matter more than the
probe's success rate: it never places, amends, or cancels anything, and it
never raises into the clock that calls it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.shioaji.order_gateway import OrderGateway


class FakeClient:
    """The surface ``warm_session`` is allowed to touch on ShioajiClient."""

    def __init__(self, api: Any, logged_in: bool = True) -> None:
        self.api = api
        self.logged_in = logged_in
        self.latency: list[tuple[str, bool]] = []

    def _record_api_latency(self, op: str, start_ns: int, ok: bool = True) -> None:
        self.latency.append((op, ok))


def _api(**kwargs: Any) -> MagicMock:
    api = MagicMock()
    for key, value in kwargs.items():
        setattr(api, key, value)
    return api


def test_a_session_that_answers_is_warm() -> None:
    api = _api()
    client = FakeClient(api)
    assert OrderGateway(client).warm_session() is True
    api.usage.assert_called_once_with()
    assert client.latency == [("warm_session", True)]


def test_a_cold_session_reports_failure_without_raising() -> None:
    """The exact production error, which the clock must survive and retry."""
    api = _api()
    api.usage.side_effect = RuntimeError(
        "Session error SolClient send request api/v1/auth/usage, code: NotReady, "
        "Error ErrorInfo { sub_code: SubCode(SessionNotEstablished) }"
    )
    client = FakeClient(api)
    assert OrderGateway(client).warm_session() is False
    assert client.latency == [("warm_session", False)]


def test_the_probe_never_touches_the_order_book() -> None:
    api = _api()
    OrderGateway(FakeClient(api)).warm_session()
    api.place_order.assert_not_called()
    api.cancel_order.assert_not_called()
    api.update_order.assert_not_called()


def test_a_client_without_an_api_is_not_probed() -> None:
    client = FakeClient(None)
    assert OrderGateway(client).warm_session() is False
    assert client.latency == []


def test_an_unauthenticated_client_is_not_probed() -> None:
    """Probing before login would turn a warm-up into a login storm."""
    api = _api()
    client = FakeClient(api, logged_in=False)
    assert OrderGateway(client).warm_session() is False
    api.usage.assert_not_called()


def test_an_sdk_without_usage_is_not_probed() -> None:
    """SDK drift must degrade to 'not warmed', not to an AttributeError."""
    api = MagicMock(spec=[])
    client = FakeClient(api)
    assert OrderGateway(client).warm_session() is False
    assert client.latency == []


def test_the_probe_does_not_serve_a_cached_usage_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cache hit is exactly the case where nothing crosses the session.

    ``AccountGateway.get_usage`` returns a cached value for
    ``HFT_USAGE_CACHE_TTL_S`` seconds. Routing the probe through it would make
    every warm-up after the first a no-op against the transport.
    """
    api = _api()
    client = FakeClient(api)
    gateway = OrderGateway(client)

    assert gateway.warm_session() is True
    assert gateway.warm_session() is True

    assert api.usage.call_count == 2


def test_the_facade_exposes_the_probe() -> None:
    """Bootstrap duck-types on this name; renaming it silently disables the clock."""
    from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

    assert callable(ShioajiClientFacade.warm_order_session)
