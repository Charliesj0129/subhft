"""The feed-gap warning must report the threshold that was actually applied.

Measured on THESHOW 2026-08-11: **695 of 695** ``Feed gap detected for symbols``
lines printed ``threshold_s: 6.0`` — the global setting — including the lines
emitted after the equity class default landed, which demonstrably judged
equities at 60 s (minimum observed stale age 61.0 s) and EXF futures at 30 s
(minimum 30.1 s). The one field a reader consults to decide whether a reported
gap is real was the one field that was never the value in force.

The cause is structural, not a typo: the warning re-derived the threshold from
a different source than the decision did. So the fix is not "read a better
attribute" — it is to make the resolved threshold travel with each stale hit,
leaving the log with nothing to re-derive.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import pytest

from hft_platform.services._md_ingestion import FeedState
from hft_platform.services._md_reconnect import MarketDataReconnectMixin

# A population with one symbol in each threshold class, shaped after the field
# window: two TSE equities on the 60 s class default, one sector future on the
# 30 s root default, one front-month index future on the strict 6 s global.
_GAPS: dict[str, float] = {
    "2207": 89.7,
    "1102": 61.5,
    "EXFH6": 49.1,
    "TXFI6": 8.0,
}
_EXPECTED_THRESHOLD: dict[str, float] = {
    "2207": 60.0,
    "1102": 60.0,
    "EXFH6": 30.0,
    "TXFI6": 6.0,
}


class _Watchdog(MarketDataReconnectMixin):
    """Minimal carrier: threshold resolution plus one watchdog cycle."""

    def __init__(self, *, gaps: dict[str, float] | None = None, grace: bool = False) -> None:
        self.running = True
        self.state = FeedState.CONNECTED
        self._grace = grace
        self._symbol_gap_threshold_s = 6.0
        self._symbol_gap_equity_threshold_s = 60.0
        self._symbol_gap_threshold_overrides: dict[str, float] = {}
        self._symbol_gap_threshold_prefix_defaults = {"EXF": 30.0}
        self._market_open_grace_s = 0.0
        self._market_open_grace_gap_threshold_s = 30.0
        # Real monotonic, as the loop reads it — patching ``time`` in a module
        # mutates the stdlib module process-wide and breaks unrelated tests
        # under xdist. The lookback is held well above the widest gap so the
        # microseconds between here and the loop cannot evict a symbol.
        self.now_mono = time.monotonic()
        self._symbol_last_tick = {s: self.now_mono - gap for s, gap in (gaps or _GAPS).items()}
        self._symbol_gap_consecutive_hits = 0
        self._watchdog_interval_s = 0.001
        self._symbol_gap_active_lookback_s = 120.0
        self._symbol_gap_min_active_symbols = 2
        # Held above the stale count so the escalation branch never runs; this
        # test is about what the warning says, not about resubscribing.
        self._symbol_gap_min_stale_count = 99

    def _is_market_open_grace_period(self) -> bool:  # type: ignore[override]
        return self._grace

    def _is_trading_hours(self) -> bool:  # type: ignore[override]
        return True


async def _run_one_cycle(watchdog: _Watchdog) -> dict[str, Any]:
    """Drive exactly one watchdog iteration; return the warning's kwargs."""
    captured: dict[str, Any] = {}

    def capture_warning(event: str, **kwargs: Any) -> None:
        if event == "Feed gap detected for symbols":
            captured.update(kwargs)

    original_sleep = asyncio.sleep

    async def stop_after_first(delay: float) -> None:
        # Ends the loop at its next condition check, so the body below runs
        # exactly once.
        watchdog.running = False
        await original_sleep(0)

    with (
        patch("hft_platform.services._md_reconnect.asyncio.sleep", stop_after_first),
        patch("hft_platform.services._md_reconnect.logger.warning", capture_warning),
    ):
        watchdog.running = True
        await watchdog._watchdog_loop()

    assert captured, "the watchdog did not emit the feed-gap warning"
    return captured


# --------------------------------------------------------------------------- #
# The decision carries its own threshold                                       #
# --------------------------------------------------------------------------- #


def test_stale_hits_carry_the_threshold_they_were_judged_against() -> None:
    watchdog = _Watchdog()
    stale = watchdog._find_stale_symbols(watchdog._symbol_last_tick, watchdog.now_mono)
    assert {symbol: threshold for symbol, _, threshold in stale} == _EXPECTED_THRESHOLD


def test_grace_period_threshold_is_the_one_carried() -> None:
    """The grace period raises the global floor to 30 s; that is what applied."""
    watchdog = _Watchdog(grace=True)
    now = watchdog.now_mono

    assert watchdog._find_stale_symbols({"TXFI6": now - 8.0}, now) == []  # under the raised floor

    stale = watchdog._find_stale_symbols({"TXFI6": now - 41.0}, now)
    assert [threshold for _, _, threshold in stale] == [30.0]


# --------------------------------------------------------------------------- #
# The warning reports it                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_warning_reports_the_resolved_thresholds_not_the_global_one() -> None:
    kwargs = await _run_one_cycle(_Watchdog())

    assert kwargs["thresholds_s"] == [6.0, 30.0, 60.0]
    # The regression itself: a scalar field reporting the global setting. It
    # read 6.0 on every one of the 695 measured lines, including those judged
    # at 60 s and 30 s.
    assert "threshold_s" not in kwargs


@pytest.mark.asyncio
async def test_each_named_symbol_reports_the_bar_it_cleared() -> None:
    """A single line names symbols from different classes, so the bar has to be
    per symbol — no one scalar can describe all of them."""
    kwargs = await _run_one_cycle(_Watchdog())

    symbols = kwargs["symbols"]
    assert "2207(89.7s>60s)" in symbols
    assert "1102(61.5s>60s)" in symbols
    assert "EXFH6(49.1s>30s)" in symbols
    assert "TXFI6(8.0s>6s)" in symbols


@pytest.mark.asyncio
async def test_warning_describes_every_stale_symbol_not_only_the_named_five() -> None:
    """``thresholds_s`` describes the population ``stale_count`` reports, which
    is wider than the five symbols the message names."""
    gaps = {f"200{i}": 70.0 for i in range(6)}
    gaps["EXFH6"] = 49.1

    kwargs = await _run_one_cycle(_Watchdog(gaps=gaps))

    assert kwargs["stale_count"] == 7
    assert kwargs["symbols"].count(",") == 4  # only five are named
    assert kwargs["thresholds_s"] == [30.0, 60.0]  # all seven are described
