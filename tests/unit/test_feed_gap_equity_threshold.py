"""TSE equities were held to a futures-grade staleness threshold.

Measured on THESHOW 2026-08-10 over 1,036,551 inter-arrivals across the 50
subscribed TSE stocks: p50 0.14 s, p90 1.83 s, p99 8.45 s, p99.9 24.02 s. The
watchdog threshold was 6.0 s — below p99 — so 5-9 stocks were "stale" at any
moment and `Feed gap detected for symbols` accounted for 1,224 of the engine's
~1,224 warnings in a 4 h window. Time-weighted expected simultaneously-stale
count at 6 s is 6.33, which is what the engine actually logged, and it sits
*above* the 5-symbol escalation floor.

That noise is not merely cosmetic: it is the channel in which the single
`track_gate_unknown_symbol_blocked` line — the one that named a two-month
trading outage — had to be noticed.

Numeric TSE codes give the product-root map nothing to key on, so the fix is a
class default. Index futures must keep the strict threshold at every month.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hft_platform.services._md_reconnect import MarketDataReconnectMixin


def _watchdog(**overrides):
    """Minimal stand-in exposing only what _find_stale_symbols reads."""
    obj = SimpleNamespace(
        _symbol_gap_threshold_s=6.0,
        _symbol_gap_equity_threshold_s=60.0,
        _symbol_gap_threshold_overrides={},
        _symbol_gap_threshold_prefix_defaults={"EXF": 30.0, "FXF": 30.0},
        _market_open_grace_gap_threshold_s=30.0,
        _is_market_open_grace_period=lambda: False,
    )
    for key, value in overrides.items():
        setattr(obj, key, value)
    obj._find_stale_symbols = MarketDataReconnectMixin._find_stale_symbols.__get__(obj)
    return obj


@pytest.mark.unit
class TestEquityClassThreshold:
    def test_equity_silent_within_the_measured_p999_is_not_stale(self) -> None:
        """24 s is p99.9 for these names — normal, not a feed problem."""
        wd = _watchdog()

        stale = wd._find_stale_symbols({"2912": 0.0, "1101": 0.0}, now=24.0)

        assert stale == []

    def test_futures_keep_the_strict_threshold(self) -> None:
        """The whole point: relaxing equities must not blind the traded product."""
        wd = _watchdog()

        stale = dict(wd._find_stale_symbols({"TMFH6": 0.0, "TXFH6": 0.0}, now=7.0))

        assert set(stale) == {"TMFH6", "TXFH6"}

    def test_equity_really_dead_is_still_caught(self) -> None:
        wd = _watchdog()

        stale = dict(wd._find_stale_symbols({"2912": 0.0}, now=61.0))

        assert "2912" in stale

    def test_exact_override_still_wins_over_the_class_default(self) -> None:
        wd = _watchdog(_symbol_gap_threshold_overrides={"2912": 10.0})

        stale = dict(wd._find_stale_symbols({"2912": 0.0, "1101": 0.0}, now=12.0))

        assert set(stale) == {"2912"}

    def test_product_root_default_still_wins_over_the_class_default(self) -> None:
        """EXF is a futures root; it must not be treated as an equity."""
        wd = _watchdog()

        stale = dict(wd._find_stale_symbols({"EXFH6": 0.0}, now=45.0))

        assert "EXFH6" in stale

    def test_market_open_grace_can_only_relax_never_tighten(self) -> None:
        """Grace raises the global to 30 s; the equity default is higher and holds."""
        wd = _watchdog(_is_market_open_grace_period=lambda: True)

        assert wd._find_stale_symbols({"2912": 0.0}, now=45.0) == []
