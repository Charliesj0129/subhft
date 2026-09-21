"""Saturday 00:00-05:00 Taipei is a live TAIFEX session, not a closed weekend.

The night session opens at 15:00 and closes at 05:00 the **next calendar day**,
so between midnight and 05:00 the wall clock says one day and the running
session belongs to the one before it. ``_within_reconnect_window`` had two
guards that read the wall-clock day and returned before the hours check -- which
already knew how to handle the tail, via ``_night_session_owner_traded`` -- ever
ran.

Measured against the production config on THESHOW
(``HFT_RECONNECT_DAYS=mon,tue,wed,thu,fri``, ``HFT_RECONNECT_HOURS=08:30-13:35``,
``HFT_RECONNECT_HOURS_2=14:55-05:05``, ``HFT_RECONNECT_TZ=Asia/Taipei``)::

    Taipei local         market open   days_until_trading   within_window
    Fri 2026-09-18 23:00     True              0                True
    Sat 2026-09-19 00:00     True              2               False   <-- X
    Sat 2026-09-19 02:30     True              2               False   <-- X
    Sat 2026-09-19 04:59     True              2               False   <-- X
    Sat 2026-09-19 05:30    False              2               False
    Wed 2026-09-16 02:30     True              0                True

``days_until_trading`` measures the distance to the *next* session and never
asks whether one is already running, so it reads Saturday -- five live trading
hours -- as further from the market than Sunday, which has none. Five hours
every Friday night in which the feed watchdog could neither reconnect nor
resubscribe.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from hft_platform.services._md_reconnect import (
    MarketDataReconnectMixin,
    _in_night_session_tail,
)

TPE = ZoneInfo("Asia/Taipei")

#: The production window set on THESHOW.
DAY_WINDOW = "08:30-13:35"
NIGHT_WINDOW = "14:55-05:05"
WEEKDAYS = {"mon", "tue", "wed", "thu", "fri"}


class _Probe(MarketDataReconnectMixin):
    """A reconnect mixin with nothing but the window configuration."""

    def __init__(self, *, days: set[str] | None = None, w1: str = DAY_WINDOW, w2: str = NIGHT_WINDOW) -> None:
        self.reconnect_days = set(WEEKDAYS if days is None else days)
        self.reconnect_hours = w1
        self.reconnect_hours_2 = w2
        self._reconnect_tzinfo = TPE


def _at(probe: Any, moment: dt.datetime) -> bool:
    with patch(
        "hft_platform.services._md_reconnect.timebase.now_s",
        return_value=moment.timestamp(),
    ):
        return probe._within_reconnect_window()


def _tpe(y: int, m: int, d: int, hh: int, mm: int = 0) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=TPE)


# 2026-09-18 is a Friday; its night session runs to 05:00 on Saturday the 19th.
FRI = (2026, 9, 18)
SAT = (2026, 9, 19)
SUN = (2026, 9, 20)
MON = (2026, 9, 21)
TUE = (2026, 9, 22)
WED = (2026, 9, 16)


class TestTheFridayNightTail:
    @pytest.mark.parametrize("hour,minute", [(0, 0), (2, 30), (4, 59)])
    def test_the_saturday_small_hours_are_inside_the_window(self, hour: int, minute: int) -> None:
        """Friday's night session is live; the watchdog must be allowed to act."""
        assert _at(_Probe(), _tpe(*SAT, hour, minute)) is True

    def test_the_friday_evening_leg_still_works(self) -> None:
        assert _at(_Probe(), _tpe(*FRI, 23, 0)) is True

    def test_the_tail_ends_when_the_session_does(self) -> None:
        """05:05 is the configured edge; 05:30 is a shut market on a Saturday."""
        assert _at(_Probe(), _tpe(*SAT, 5, 30)) is False

    def test_saturday_daytime_is_still_closed(self) -> None:
        assert _at(_Probe(), _tpe(*SAT, 10, 0)) is False
        assert _at(_Probe(), _tpe(*SAT, 23, 0)) is False


class TestTheDaysThatMustStayClosed:
    def test_sunday_is_closed_all_day(self) -> None:
        """Nothing opened on Saturday, so Sunday has no tail to inherit."""
        for hour in (0, 2, 4, 12, 18, 23):
            assert _at(_Probe(), _tpe(*SUN, hour)) is False

    def test_monday_small_hours_are_closed(self) -> None:
        """Monday 00:00-05:00 would be Sunday night's tail, and Sunday has none."""
        for hour in (0, 2, 4):
            assert _at(_Probe(), _tpe(*MON, hour)) is False

    def test_monday_daytime_is_open(self) -> None:
        assert _at(_Probe(), _tpe(*MON, 9, 0)) is True

    def test_a_midweek_tail_was_never_broken_and_still_works(self) -> None:
        """Tuesday 02:30 belongs to Monday night; this was already correct."""
        assert _at(_Probe(), _tpe(*TUE, 2, 30)) is True
        assert _at(_Probe(), _tpe(*WED, 2, 30)) is True


class TestTheTailDoesNotOverrideEverything:
    def test_a_window_set_with_no_cross_midnight_leg_has_no_tail(self) -> None:
        probe = _Probe(w1=DAY_WINDOW, w2="")
        assert _at(probe, _tpe(*SAT, 2, 30)) is False

    def test_the_tail_respects_the_configured_end(self) -> None:
        """A shorter night window must shorten the tail with it."""
        probe = _Probe(w2="14:55-02:00")
        assert _at(probe, _tpe(*SAT, 1, 30)) is True
        assert _at(probe, _tpe(*SAT, 3, 30)) is False

    def test_an_unconfigured_service_still_allows_everything(self) -> None:
        probe = _Probe(days=set(), w1="", w2="")
        assert _at(probe, _tpe(*SUN, 3, 0)) is True

    def test_a_malformed_window_is_ignored_not_treated_as_a_tail(self) -> None:
        probe = _Probe(w1=DAY_WINDOW, w2="not-a-window")
        assert _at(probe, _tpe(*SAT, 2, 30)) is False


class TestTheTailPredicate:
    def test_it_recognises_the_saturday_small_hours(self) -> None:
        assert _in_night_session_tail(_tpe(*SAT, 2, 30), [DAY_WINDOW, NIGHT_WINDOW]) is True

    def test_it_rejects_the_evening_leg(self) -> None:
        """The evening leg is unconditional and belongs to the window loop."""
        assert _in_night_session_tail(_tpe(*FRI, 23, 0), [NIGHT_WINDOW]) is False

    def test_it_rejects_a_tail_whose_session_never_opened(self) -> None:
        assert _in_night_session_tail(_tpe(*MON, 2, 30), [NIGHT_WINDOW]) is False

    def test_a_same_day_window_has_no_tail(self) -> None:
        assert _in_night_session_tail(_tpe(*SAT, 2, 30), [DAY_WINDOW]) is False

    def test_no_windows_means_no_tail(self) -> None:
        assert _in_night_session_tail(_tpe(*SAT, 2, 30), []) is False

    def test_a_malformed_window_is_skipped(self) -> None:
        assert _in_night_session_tail(_tpe(*SAT, 2, 30), ["garbage", NIGHT_WINDOW]) is True

    def test_it_fails_open_when_the_calendar_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A calendar outage must never suppress reconnects during a session."""
        import hft_platform.core.market_calendar as market_calendar

        def _boom() -> Any:
            raise RuntimeError("calendar unavailable")

        monkeypatch.setattr(market_calendar, "get_calendar", _boom)
        assert _in_night_session_tail(_tpe(*SAT, 2, 30), [NIGHT_WINDOW]) is True
