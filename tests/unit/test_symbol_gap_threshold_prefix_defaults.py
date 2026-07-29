"""Product-root defaults for the per-symbol feed-gap watchdog.

A flat 6 s threshold against structurally illiquid TAIFEX sector futures
produced ~2700 warnings/day on a single contract (EXFH6 quoting every
6.1-6.4 s) at a stale_ratio of 0.005 — one symbol out of ~180 active. That is
not a fault, it is noise, and noise at that volume hides the real gap it is
supposed to surface.

The exact-symbol override map already existed but keys on a full symbol, which
rolls every quarter (EXFH6 → EXFJ6), so any hand-set override silently expires.
Root-keyed defaults survive the roll. Front-month index futures must keep the
strict global threshold.
"""

from __future__ import annotations

from typing import Any

import pytest

from hft_platform.services._md_reconnect import MarketDataReconnectMixin, _prefix_gap_threshold


class _Watchdog(MarketDataReconnectMixin):
    """Minimal carrier for the threshold-resolution attributes."""

    def __init__(
        self,
        *,
        global_threshold: float = 6.0,
        overrides: dict[str, float] | None = None,
        prefix_defaults: dict[str, float] | None = None,
        grace: bool = False,
    ) -> None:
        self._symbol_gap_threshold_s = global_threshold
        self._symbol_gap_threshold_overrides = overrides or {}
        self._symbol_gap_threshold_prefix_defaults = prefix_defaults if prefix_defaults is not None else {"EXF": 30.0}
        self._market_open_grace_gap_threshold_s = 30.0
        self._grace = grace

    def _is_market_open_grace_period(self) -> bool:  # type: ignore[override]
        return self._grace


def _stale(watchdog: Any, symbol: str, gap: float) -> bool:
    now = 1_000.0
    return bool(watchdog._find_stale_symbols({symbol: now - gap}, now))


# --------------------------------------------------------------------------- #
# Longest-prefix lookup                                                        #
# --------------------------------------------------------------------------- #


def test_prefix_lookup_returns_none_without_a_match():
    assert _prefix_gap_threshold("TXFF6", {"EXF": 30.0}) is None


def test_prefix_lookup_returns_the_matching_root():
    assert _prefix_gap_threshold("EXFH6", {"EXF": 30.0}) == 30.0


def test_prefix_lookup_prefers_the_longest_match():
    assert _prefix_gap_threshold("EXFA1H6", {"EXF": 30.0, "EXFA": 90.0}) == 90.0


def test_prefix_lookup_is_a_noop_for_an_empty_map():
    assert _prefix_gap_threshold("EXFH6", {}) is None


# --------------------------------------------------------------------------- #
# Threshold resolution in the watchdog                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("symbol", ["EXFH6", "EXFJ6", "EXFF6"])
def test_gap_threshold_prefix_default_applies_across_the_quarterly_roll(symbol):
    """The root default has to hold for whichever month is trading."""
    watchdog = _Watchdog()
    assert _stale(watchdog, symbol, 6.5) is False  # was a warning at the 6 s global
    assert _stale(watchdog, symbol, 31.0) is True  # a genuine gap still trips


def test_front_month_index_future_keeps_the_strict_global_threshold():
    """Relaxing the noise must not relax the instrument that actually matters."""
    watchdog = _Watchdog()
    assert _stale(watchdog, "TXFF6", 6.5) is True
    assert _stale(watchdog, "TMFF6", 6.5) is True
    assert _stale(watchdog, "MXFF6", 6.5) is True


def test_far_month_index_future_also_keeps_the_strict_threshold():
    """Root prefixes cannot tell TXFI6 from TXFF6 — both stay strict by design."""
    watchdog = _Watchdog()
    assert _stale(watchdog, "TXFI6", 6.5) is True


def test_env_override_wins_over_prefix_default():
    """Ops keep the last word in both directions."""
    relaxed = _Watchdog(overrides={"EXFH6": 120.0})
    assert _stale(relaxed, "EXFH6", 60.0) is False

    tightened = _Watchdog(overrides={"EXFH6": 5.0})
    assert _stale(tightened, "EXFH6", 6.0) is True


def test_market_open_grace_never_tightens_a_root_default():
    """Grace raises the global floor; it must not drag a relaxed root down."""
    watchdog = _Watchdog(prefix_defaults={"EXF": 90.0}, grace=True)
    assert _stale(watchdog, "EXFH6", 60.0) is False
    # the grace floor still applies to everything else
    assert _stale(watchdog, "TXFF6", 20.0) is False
    assert _stale(watchdog, "TXFF6", 31.0) is True


def test_options_stay_excluded_from_the_watchdog_entirely():
    watchdog = _Watchdog()
    assert _stale(watchdog, "TXO24400F6", 600.0) is False


def test_no_prefix_defaults_configured_falls_back_to_the_global_threshold():
    watchdog = _Watchdog(prefix_defaults={})
    assert _stale(watchdog, "EXFH6", 6.5) is True
