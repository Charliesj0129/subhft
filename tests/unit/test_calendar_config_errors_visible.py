"""A bad calendar setting must be seen, and must not make the calendar disappear.

``HFT_CALENDAR_CACHE_SIZE=`` (set but empty) made ``MarketCalendar()`` raise ``ValueError``. The supervisor's
calendar read swallowed it at DEBUG and reported "market shut", which switches off the feed-gap and pending-reconnect
reduce-only reasons -- an engine with a dead feed that looked healthy. A timezone that fails to resolve fell back
without a word, and could disagree with ``timebase`` about which day it is.
"""

from __future__ import annotations

import datetime as dt
import threading
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from hft_platform.core import market_calendar, session_hooks, timebase
from hft_platform.core.market_calendar import MarketCalendar, reset_calendar
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.ops import platform_inputs

_TAIPEI = ZoneInfo("Asia/Taipei")


@pytest.fixture(autouse=True)
def _fresh_calendar_state(monkeypatch):
    reset_calendar()
    monkeypatch.setattr(platform_inputs, "_calendar_fault_seen", None)
    monkeypatch.setattr(session_hooks, "_tz_fallback_warned", False)
    yield
    reset_calendar()


def _logged(logger_mock: MagicMock, level: str) -> list[str]:
    return [call.args[0] for call in getattr(logger_mock, level).call_args_list]


# ── HFT_CALENDAR_CACHE_SIZE ──────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["", "   ", "abc", "3.5", "-1"])
def test_an_unusable_cache_size_builds_the_calendar_with_the_default(monkeypatch, raw):
    monkeypatch.setenv("HFT_CALENDAR_CACHE_SIZE", raw)

    calendar = MarketCalendar()

    assert calendar._session_cache_size == market_calendar._DEFAULT_SESSION_CACHE_SIZE


@pytest.mark.parametrize(("raw", "expected"), [("8", 8), (" 16 ", 16), ("0", 0)])
def test_a_valid_cache_size_is_used(monkeypatch, raw, expected):
    monkeypatch.setenv("HFT_CALENDAR_CACHE_SIZE", raw)

    assert MarketCalendar()._session_cache_size == expected


def test_an_unset_cache_size_is_the_default_and_says_nothing(monkeypatch):
    monkeypatch.delenv("HFT_CALENDAR_CACHE_SIZE", raising=False)
    logger = MagicMock()
    monkeypatch.setattr(market_calendar, "logger", logger)

    calendar = MarketCalendar()

    assert calendar._session_cache_size == market_calendar._DEFAULT_SESSION_CACHE_SIZE
    assert "market_calendar_cache_size_invalid" not in _logged(logger, "warning")


def test_an_unusable_cache_size_is_reported_at_warning(monkeypatch):
    monkeypatch.setenv("HFT_CALENDAR_CACHE_SIZE", "")
    logger = MagicMock()
    monkeypatch.setattr(market_calendar, "logger", logger)

    MarketCalendar()

    assert _logged(logger, "warning").count("market_calendar_cache_size_invalid") == 1


def test_the_supervisor_reads_seconds_since_open_when_the_cache_size_is_empty(monkeypatch):
    """The N142 chain end to end: an empty setting used to turn this into None for the whole session."""
    monkeypatch.setenv("HFT_CALENDAR_CACHE_SIZE", "")
    monkeypatch.setattr(MarketCalendar, "_is_futures_trading_hours", lambda self, ts: True)

    assert platform_inputs._seconds_since_session_open() is not None


# ── the supervisor's calendar read ───────────────────────────────────────


def _failing_calendar(monkeypatch, message: str) -> None:
    def boom():
        raise RuntimeError(message)

    monkeypatch.setattr(market_calendar, "get_calendar", boom)


def _unavailable() -> float:
    return MetricsRegistry.get().platform_inputs_calendar_unavailable_total._value.get()


def test_every_unreadable_calendar_tick_is_counted_and_reads_as_shut(monkeypatch):
    _failing_calendar(monkeypatch, "boom")
    before = _unavailable()

    results = [platform_inputs._seconds_since_session_open() for _ in range(3)]

    assert results == [None, None, None]
    assert _unavailable() == before + 3


def test_a_persistent_fault_is_warned_once_and_a_new_one_warns_again(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(platform_inputs, "logger", logger)
    _failing_calendar(monkeypatch, "first")
    for _ in range(3):
        platform_inputs._seconds_since_session_open()
    assert _logged(logger, "warning") == ["platform_inputs_calendar_unavailable"]

    _failing_calendar(monkeypatch, "second")
    platform_inputs._seconds_since_session_open()

    assert _logged(logger, "warning") == ["platform_inputs_calendar_unavailable"] * 2


def test_a_recovered_calendar_warns_again_when_it_fails_again(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(platform_inputs, "logger", logger)
    _failing_calendar(monkeypatch, "same")
    platform_inputs._seconds_since_session_open()

    monkeypatch.setattr(
        market_calendar, "get_calendar", lambda: MagicMock(seconds_since_futures_session_open=lambda: 5.0)
    )
    assert platform_inputs._seconds_since_session_open() == 5.0
    _failing_calendar(monkeypatch, "same")
    platform_inputs._seconds_since_session_open()

    assert _logged(logger, "warning") == ["platform_inputs_calendar_unavailable"] * 2


# ── HFT_TS_TZ ────────────────────────────────────────────────────────────


def test_an_invalid_timezone_falls_back_to_utc_plus_8_and_says_so(monkeypatch):
    monkeypatch.setenv("HFT_TS_TZ", "Asia/Taipai")
    monkeypatch.setattr(timebase, "TZINFO", _TAIPEI)  # isolate: timebase itself is not what is under test
    logger = MagicMock()
    monkeypatch.setattr(market_calendar, "logger", logger)

    calendar = MarketCalendar()

    assert calendar._tz.utcoffset(None) == dt.timedelta(hours=8)
    assert "market_calendar_timezone_invalid" in _logged(logger, "warning")


def test_a_calendar_that_disagrees_with_timebase_is_reported(monkeypatch):
    """The N141 case: ``timebase`` falls back to UTC while the calendar falls back to UTC+8."""
    monkeypatch.setenv("HFT_TS_TZ", "Asia/Taipai")
    monkeypatch.setattr(timebase, "TZINFO", dt.timezone.utc)
    logger = MagicMock()
    monkeypatch.setattr(market_calendar, "logger", logger)

    calendar = MarketCalendar()

    assert "market_calendar_timezone_disagrees_with_timebase" in _logged(logger, "warning")
    assert calendar._warn_if_timezone_disagrees_with_timebase() is True


def test_a_calendar_that_agrees_with_timebase_reports_nothing(monkeypatch):
    monkeypatch.setenv("HFT_TS_TZ", "Asia/Taipei")
    monkeypatch.setattr(timebase, "TZINFO", _TAIPEI)
    logger = MagicMock()
    monkeypatch.setattr(market_calendar, "logger", logger)

    calendar = MarketCalendar()

    assert calendar._warn_if_timezone_disagrees_with_timebase() is False
    assert "market_calendar_timezone_disagrees_with_timebase" not in _logged(logger, "warning")


def test_session_hooks_report_an_invalid_timezone_once(monkeypatch):
    monkeypatch.setenv("HFT_TS_TZ", "Asia/Taipai")
    logger = MagicMock()
    monkeypatch.setattr(session_hooks, "logger", logger)
    hooks = session_hooks.SessionHookManager()
    hooks._calendar = MagicMock()
    hooks._calendar.is_trading_hours.return_value = False
    hooks._calendar.is_trading_day.return_value = False

    for _ in range(3):
        phase = hooks._detect_phase()

    assert phase == session_hooks.SessionPhase.PRE_MARKET
    assert _logged(logger, "warning") == ["session_hooks_timezone_invalid"]


def test_session_hooks_say_nothing_for_a_valid_timezone(monkeypatch):
    monkeypatch.setenv("HFT_TS_TZ", "Asia/Taipei")
    logger = MagicMock()
    monkeypatch.setattr(session_hooks, "logger", logger)
    hooks = session_hooks.SessionHookManager()
    hooks._calendar = MagicMock()
    hooks._calendar.is_trading_hours.return_value = False
    hooks._calendar.is_trading_day.return_value = False

    hooks._detect_phase()

    assert _logged(logger, "warning") == []


def test_the_once_flag_is_checked_and_set_while_holding_a_lock(monkeypatch):
    """A second thread must wait for the first to finish the check-and-set, so it cannot also pass the check."""
    logger = MagicMock()
    monkeypatch.setattr(session_hooks, "logger", logger)
    entered = threading.Event()

    def poll() -> None:
        entered.set()
        session_hooks._warn_timezone_fallback_once("Asia/Taipai", ValueError("unknown zone"))

    worker = threading.Thread(target=poll)
    with session_hooks._tz_fallback_lock:
        worker.start()
        assert entered.wait(timeout=5)
        worker.join(timeout=0.05)  # bounded wait for a thread that must be blocked on the lock
        assert worker.is_alive()
        assert session_hooks._tz_fallback_warned is False
        assert _logged(logger, "warning") == []

    worker.join(timeout=5)
    assert not worker.is_alive()
    assert _logged(logger, "warning") == ["session_hooks_timezone_invalid"]


def test_concurrent_polls_report_an_invalid_timezone_once(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(session_hooks, "logger", logger)
    start = threading.Barrier(8)

    def poll() -> None:
        start.wait(timeout=5)
        session_hooks._warn_timezone_fallback_once("Asia/Taipai", ValueError("unknown zone"))

    workers = [threading.Thread(target=poll) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert not any(worker.is_alive() for worker in workers)
    assert _logged(logger, "warning") == ["session_hooks_timezone_invalid"]
