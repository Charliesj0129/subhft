"""The reduce-only feed gap watches the liquid roots, not every latched future.

Production shape (THESHOW 2026-10-30 20:20:32Z, 04:20 Taipei): EXFK6 had crossed
the 5-event latch, then went 600 s without an event while the feed as a whole
carried ~3,500 events/min. ``get_active_feed_gap_s`` takes the max over every
latched future, so one illiquid contract put the platform in reduce-only
(``feed_reconnect_unhealthy``). Over 2026-10-05..30 EXF went quiet >= 300 s inside
a session 9 times on its own; every such gap on TXF/MXF/TMF hit all of them at
once. These tests pin both halves: a silent illiquid root no longer moves the
signal, and a silent liquid root still does.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.services.market_data import MarketDataService, _parse_feed_gap_roots

_ROOTS_ENV = "HFT_PLATFORM_REDUCE_ONLY_FEED_GAP_ROOTS"
_FRONT_MONTHS = {"TXFK6", "MXFK6", "TMFK6", "EXFK6"}


def _make_service(roots_env: str | None = None) -> MarketDataService:
    from hft_platform.engine.event_bus import RingBufferBus

    tmp = tempfile.TemporaryDirectory()
    cfg = Path(tmp.name) / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    env = {
        "HFT_FEATURE_ENGINE_ENABLED": "0",
        "SYMBOLS_CONFIG": str(cfg),
        "HFT_MONITOR_LIVE_ENABLED": "0",
    }
    client = MagicMock()
    client.subscribed_codes = set(_FRONT_MONTHS)
    client.alias_to_actual = {}
    with patch.dict(os.environ, env):
        os.environ.pop(_ROOTS_ENV, None)
        if roots_env is not None:
            os.environ[_ROOTS_ENV] = roots_env
        svc = MarketDataService(MagicMock(spec=RingBufferBus), asyncio.Queue(maxsize=100), client)
    svc._tmp = tmp  # keep the symbols file alive with the service
    return svc


def _latch(svc: MarketDataService, silence_s: dict[str, float]) -> None:
    now = time.monotonic()
    svc._symbol_last_tick = {symbol: now - age for symbol, age in silence_s.items()}
    svc._ever_active_symbols = set(silence_s)


def test_feed_gap_ignores_a_silent_illiquid_root_while_the_liquid_roots_flow() -> None:
    svc = _make_service()
    _latch(svc, {"TXFK6": 0.4, "MXFK6": 0.2, "TMFK6": 0.1, "EXFK6": 631.0})

    gap = svc.get_active_feed_gap_s()

    assert gap < 5.0, f"EXFK6 silence reached the reduce-only signal: {gap=}"


def test_feed_gap_still_reports_a_silent_liquid_root_in_full() -> None:
    svc = _make_service()
    _latch(svc, {"TXFK6": 0.4, "MXFK6": 0.2, "TMFK6": 700.0, "EXFK6": 0.3})

    gap = svc.get_active_feed_gap_s()

    assert gap >= 690.0, f"TMFK6 outage masked: {gap=}"


def test_feed_gap_counts_every_month_of_a_liquid_root() -> None:
    svc = _make_service()
    svc.client.subscribed_codes = _FRONT_MONTHS | {"TXFA7"}
    _latch(svc, {"TXFK6": 0.4, "TXFA7": 650.0, "TMFK6": 0.1})

    gap = svc.get_active_feed_gap_s()

    assert gap >= 640.0, f"far-month TXFA7 silence dropped: {gap=}"


def test_feed_gap_roots_env_adds_a_root_and_drops_the_rest() -> None:
    svc = _make_service(roots_env="txf, EXF")
    _latch(svc, {"TXFK6": 0.4, "TMFK6": 900.0, "EXFK6": 631.0})

    gap = svc.get_active_feed_gap_s()

    assert svc._feed_gap_health_roots == ("TXF", "EXF")
    assert 620.0 <= gap < 640.0, f"expected EXFK6's 631 s, not TMFK6's 900 s: {gap=}"


@pytest.mark.parametrize("raw", ["", " ", " , ,"])
def test_feed_gap_blank_roots_env_keeps_the_default_not_every_root(raw: str) -> None:
    svc = _make_service(roots_env=raw)
    _latch(svc, {"TXFK6": 0.4, "TMFK6": 0.1, "EXFK6": 631.0})

    gap = svc.get_active_feed_gap_s()

    assert svc._feed_gap_health_roots == ("TXF", "MXF", "TMF")
    assert gap < 5.0, f"a blank override read as a wildcard: {gap=}"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("TXF;MXF", ("TXF", "MXF", "TMF")), ("TX F,TMF", ("TMF",)), ("TMF,TXFK6TOOLONG", ("TMF",))],
)
def test_feed_gap_roots_env_drops_a_token_that_cannot_be_a_root(raw: str, expected: tuple[str, ...]) -> None:
    svc = _make_service(roots_env=raw)
    _latch(svc, {"TXFK6": 0.4, "TMFK6": 900.0})

    gap = svc.get_active_feed_gap_s()

    assert svc._feed_gap_health_roots == expected
    assert gap >= 890.0, f"a mistyped override switched the TMFK6 outage off: {gap=}"


def test_feed_gap_still_delatches_an_unsubscribed_illiquid_root() -> None:
    svc = _make_service()
    svc.client.subscribed_codes = {"TXFK6", "TMFK6"}
    _latch(svc, {"TXFK6": 0.4, "TMFK6": 0.1, "EXFK6": 631.0})

    svc.get_active_feed_gap_s()

    assert "EXFK6" not in svc._ever_active_symbols
    assert {"TXFK6", "TMFK6"} <= svc._ever_active_symbols


def test_feed_gap_is_zero_while_no_liquid_root_has_latched() -> None:
    svc = _make_service()
    _latch(svc, {"EXFK6": 631.0})

    assert svc.get_active_feed_gap_s() == 0.0


def test_parse_feed_gap_roots_dedups_and_upper_cases() -> None:
    assert _parse_feed_gap_roots("tmf,TMF, mxf", ("TXF",)) == ("TMF", "MXF")
    assert _parse_feed_gap_roots(None, ("TXF",)) == ("TXF",)
