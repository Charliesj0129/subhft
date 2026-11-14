"""Weekly TXO codes are options to the feed-gap watchdog, like the monthly TXO.

The weekly roots (TX1..TX5, TXU..TXZ) share no prefix with "TXO". Left out of
the exclusion lists, their far strikes, which can go a minute without a quote
in a night session, were judged at the 6 s futures threshold and could push the
watchdog into a pool-wide resubscribe that also hits the R47 TMF feed.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from hft_platform.config._symbols_expansion import TXO_WEEKLY_ROOTS
from hft_platform.services._md_reconnect import _WATCHDOG_EXCLUDE_PREFIXES, MarketDataReconnectMixin
from hft_platform.services.market_data import MarketDataService

WEEKLY_CODES = ("TX145350I6", "TX245000U6", "TXU44900U6", "TXV45100I6")


@pytest.mark.parametrize("root", TXO_WEEKLY_ROOTS)
def test_watchdog_excludes_every_weekly_root(root: str) -> None:
    assert root in _WATCHDOG_EXCLUDE_PREFIXES


def test_feed_gap_and_watchdog_exclusions_are_the_same_tuple() -> None:
    assert MarketDataService._FEED_GAP_EXCLUDE_PREFIXES == _WATCHDOG_EXCLUDE_PREFIXES
    assert {"TXO", "MXO", "TEO", "TFO"} <= set(_WATCHDOG_EXCLUDE_PREFIXES)


def test_exclusions_do_not_swallow_the_futures_r47_trades() -> None:
    for code in ("TXFK6", "TMFK6", "MXFK6", "EXFK6"):
        assert not any(code.startswith(p) for p in _WATCHDOG_EXCLUDE_PREFIXES), code


def _watchdog_host() -> SimpleNamespace:
    host = SimpleNamespace(
        _symbol_gap_threshold_s=6.0,
        _symbol_gap_threshold_overrides={},
        _symbol_gap_threshold_prefix_defaults={},
        _symbol_gap_equity_threshold_s=60.0,
        _is_market_open_grace_period=lambda: False,
    )
    return host


def test_stale_weekly_options_are_not_flagged_by_the_watchdog() -> None:
    now = time.monotonic()
    snapshot = {code: now - 90.0 for code in WEEKLY_CODES}
    snapshot["TMFK6"] = now - 90.0

    stale = MarketDataReconnectMixin._find_stale_symbols(_watchdog_host(), snapshot, now)

    assert [symbol for symbol, _gap, _threshold in stale] == ["TMFK6"]
