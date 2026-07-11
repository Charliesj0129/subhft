"""Dual-version Shioaji compatibility tests.

The adapter must run correctly on both the pinned 1.3.3 SDK (current
production) and the held 1.5.3 upgrade (PR #367). Between those releases the
quote enums moved from ``sj.constant.*`` to top-level ``sj.*`` and the quote
API surface (subscribe/unsubscribe/setters/event-callback) moved from the
``api.quote`` proxy to the top-level ``api``; both old locations remain in
1.5.3 only as deprecation-warning shims. 1.5.3 also drops the v0 setters
entirely and switches the v1 quote callbacks from a 2-arg ``(exchange, data)``
signature to 1-arg ``(data)``.

These tests pin the feature-detected resolvers and the dispatch arity bridge
using plain synthetic objects (no MagicMock), so ``hasattr`` reflects the real
SDK shapes rather than MagicMock's auto-attribute behaviour.
"""

from __future__ import annotations

import types

import pytest

import hft_platform.feed_adapter.shioaji.client as client_mod
from hft_platform.feed_adapter.shioaji._compat import (
    resolve_quote_api,
    resolve_quote_enum,
)

# --- synthetic SDK shapes (plain objects, real hasattr) ---------------------


def _sj_153() -> types.SimpleNamespace:
    """1.5.3: enums top-level (and a deprecated .constant shim still present)."""
    qt = types.SimpleNamespace(Tick="tick", BidAsk="bidask")
    qv = types.SimpleNamespace(v0="v0", v1="v1")
    const = types.SimpleNamespace(QuoteType=qt, QuoteVersion=qv)
    return types.SimpleNamespace(QuoteType=qt, QuoteVersion=qv, constant=const)


def _sj_133() -> types.SimpleNamespace:
    """1.3.3: enums ONLY under .constant (no top-level)."""
    const = types.SimpleNamespace(
        QuoteType=types.SimpleNamespace(Tick="tick", BidAsk="bidask"),
        QuoteVersion=types.SimpleNamespace(v0="v0", v1="v1"),
    )
    return types.SimpleNamespace(constant=const)


def _api_153() -> types.SimpleNamespace:
    """1.5.3 api: subscribe/unsubscribe (and everything) live top-level; a
    deprecated api.quote proxy still exposes them too."""
    methods = {
        "subscribe": lambda *a, **k: None,
        "unsubscribe": lambda *a, **k: None,
        "set_on_tick_stk_v1_callback": lambda *a, **k: None,
        "set_event_callback": lambda *a, **k: None,
    }
    api = types.SimpleNamespace(**methods)
    api.quote = types.SimpleNamespace(**methods)
    return api


def _api_133() -> types.SimpleNamespace:
    """1.3.3 api: subscribe/unsubscribe ONLY on the api.quote proxy."""
    proxy = types.SimpleNamespace(
        subscribe=lambda *a, **k: None,
        unsubscribe=lambda *a, **k: None,
        set_on_tick_stk_v1_callback=lambda *a, **k: None,
        set_event_callback=lambda *a, **k: None,
    )
    return types.SimpleNamespace(quote=proxy)


# --- resolve_quote_enum -----------------------------------------------------


def test_resolve_quote_enum_prefers_top_level_on_153():
    sj = _sj_153()
    assert resolve_quote_enum(sj, "QuoteType") is sj.QuoteType
    assert resolve_quote_enum(sj, "QuoteVersion") is sj.QuoteVersion


def test_resolve_quote_enum_falls_back_to_constant_on_133():
    sj = _sj_133()
    assert resolve_quote_enum(sj, "QuoteType") is sj.constant.QuoteType
    assert resolve_quote_enum(sj, "QuoteVersion") is sj.constant.QuoteVersion


def test_resolve_quote_enum_raises_when_absent():
    with pytest.raises(AttributeError):
        resolve_quote_enum(types.SimpleNamespace(), "QuoteType")


# --- resolve_quote_api ------------------------------------------------------


def test_resolve_quote_api_prefers_top_level_on_153():
    api = _api_153()
    assert resolve_quote_api(api) is api


def test_resolve_quote_api_falls_back_to_quote_proxy_on_133():
    api = _api_133()
    assert resolve_quote_api(api) is api.quote


def test_resolve_quote_api_none_when_api_is_none():
    assert resolve_quote_api(None) is None


def test_resolve_quote_api_none_when_no_subscribe_surface():
    # Logged-out / malformed api with neither top-level subscribe nor a usable proxy.
    assert resolve_quote_api(types.SimpleNamespace()) is None
    assert resolve_quote_api(types.SimpleNamespace(quote=types.SimpleNamespace())) is None


# --- dispatch_tick_cb arity bridge ------------------------------------------
# 1.5.3 invokes v1 quote callbacks with a single (data) arg; 1.3.3 invokes them
# with (topic, data). The router hot path is a concrete 2-arg function, so the
# client wrapper must normalise both arities or a 1-arg call raises TypeError on
# the hot path before the router's own try/except can catch it.


def test_dispatch_tick_cb_bridges_single_arg_v1(monkeypatch):
    seen = []
    monkeypatch.setattr(client_mod._router, "dispatch_tick_cb", lambda topic, quote: seen.append((topic, quote)))
    tick = types.SimpleNamespace(code="2330")
    client_mod.dispatch_tick_cb(tick)  # 1-arg (1.5.3 v1)
    client_mod.dispatch_tick_cb("L/TFE/TXF", tick)  # 2-arg (1.3.3)
    assert seen == [(None, tick), ("L/TFE/TXF", tick)]


def test_dispatch_tick_cb_single_arg_does_not_raise(monkeypatch):
    # Pre-fix a 1-arg call raised TypeError (missing 'quote') before the router
    # body's try/except — disconnecting the feed on the first 1.5.3 tick.
    monkeypatch.setattr(client_mod._router, "dispatch_tick_cb", lambda topic, quote: None)
    client_mod.dispatch_tick_cb(types.SimpleNamespace(code="2330"))  # must not raise
