"""Fubon futopt session flag: day session 08:45-13:45 (Taipei), night session otherwise.

TAIFEX has no all-day session, so the platform sets afterHours itself: on subscribe
and again at each day-session boundary. These tests pin the clock; none reads wall time.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Iterator
from unittest.mock import MagicMock, call
from zoneinfo import ZoneInfo

import pytest

from tests.unit.fubon_mock_helper import install_fubon_neo_mock

install_fubon_neo_mock()

from hft_platform.feed_adapter.fubon import subscription_manager as subscription_manager_module
from hft_platform.feed_adapter.fubon.subscription_manager import (
    FubonSubscriptionManager,
    futopt_after_hours,
    next_session_boundary,
)

_TAIPEI = ZoneInfo("Asia/Taipei")
# Managers opened by a test; closed afterwards so no session loop outlives its test.
_OPEN: list[FubonSubscriptionManager] = []


def _at(hour: int, minute: int = 0, second: int = 0, *, day: int = 24) -> dt.datetime:
    return dt.datetime(2026, 11, day, hour, minute, second, tzinfo=_TAIPEI)


class _Clock:
    """A settable session clock."""

    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def _cb(*args, **kwargs) -> None:
    return None


@pytest.fixture(autouse=True)
def _close_session_loops() -> Iterator[None]:
    yield
    for manager in _OPEN:
        manager.close()
    _OPEN.clear()


def _manager(clock: _Clock) -> tuple[FubonSubscriptionManager, MagicMock]:
    quote_rt = MagicMock(name="quote_runtime")
    manager = FubonSubscriptionManager(quote_rt, ["TXFL6"], session_clock=clock)
    _OPEN.append(manager)
    return manager, quote_rt


@pytest.mark.parametrize(
    ("when", "after_hours"),
    [
        (_at(4, 59, 59), True),
        (_at(5, 0), True),
        (_at(8, 44, 59), True),
        (_at(8, 45), False),
        (_at(10, 0), False),
        (_at(13, 44, 59), False),
        (_at(13, 45), True),
        (_at(15, 0), True),
        (_at(23, 59, 59), True),
    ],
    ids=[
        "before-open",
        "after-close",
        "pre-open-last-second",
        "day-open-inclusive",
        "day-mid",
        "day-last-second",
        "day-close-exclusive",
        "night-open",
        "night-late",
    ],
)
def test_futopt_after_hours_follows_day_window(when: dt.datetime, after_hours: bool) -> None:
    assert futopt_after_hours(when) is after_hours


@pytest.mark.parametrize(
    ("now", "boundary"),
    [
        (_at(4, 0), _at(8, 45)),
        (_at(8, 45), _at(13, 45)),
        (_at(10, 0), _at(13, 45)),
        (_at(13, 45), _at(8, 45, day=25)),
        (_at(16, 0), _at(8, 45, day=25)),
        (_at(23, 30), _at(8, 45, day=25)),
    ],
    ids=["early-morning", "day-open-edge", "day-mid", "day-close-edge", "night", "late-night"],
)
def test_next_session_boundary_is_first_edge_after_now(now: dt.datetime, boundary: dt.datetime) -> None:
    assert next_session_boundary(now) == boundary


def test_session_helpers_refuse_naive_datetime() -> None:
    naive = dt.datetime(2026, 11, 24, 10, 0)
    with pytest.raises(ValueError, match="timezone"):
        futopt_after_hours(naive)
    with pytest.raises(ValueError, match="timezone"):
        next_session_boundary(naive)


def test_subscribe_basket_subscribes_day_flag_during_day_session() -> None:
    mgr, quote_rt = _manager(_Clock(_at(10, 0)))
    mgr.subscribe_basket(_cb)
    quote_rt.subscribe.assert_called_once_with(["TXFL6"], after_hours=False)


def test_subscribe_basket_subscribes_night_flag_during_night_session() -> None:
    mgr, quote_rt = _manager(_Clock(_at(16, 0)))
    mgr.subscribe_basket(_cb)
    quote_rt.subscribe.assert_called_once_with(["TXFL6"], after_hours=True)


def test_subscribe_basket_without_session_clock_keeps_day_flag() -> None:
    quote_rt = MagicMock(name="quote_runtime")
    mgr = FubonSubscriptionManager(quote_rt, ["TXFL6"])
    _OPEN.append(mgr)
    mgr.subscribe_basket(_cb)
    quote_rt.subscribe.assert_called_once_with(["TXFL6"], after_hours=False)


def test_sync_session_resubscribes_with_night_flag_at_day_close() -> None:
    clock = _Clock(_at(13, 44, 30))
    mgr, quote_rt = _manager(clock)
    mgr.subscribe_basket(_cb)

    clock.now = _at(13, 45, 0)

    assert mgr.sync_session() is True
    quote_rt.unsubscribe.assert_called_once_with(["TXFL6"])
    assert quote_rt.subscribe.call_args_list[-1] == call(["TXFL6"], after_hours=True)


def test_sync_session_does_nothing_inside_same_session() -> None:
    clock = _Clock(_at(10, 0))
    mgr, quote_rt = _manager(clock)
    mgr.subscribe_basket(_cb)

    clock.now = _at(11, 30)

    assert mgr.sync_session() is False
    quote_rt.unsubscribe.assert_not_called()
    assert quote_rt.subscribe.call_count == 1


def test_session_loop_switches_flag_when_clock_crosses_day_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subscription_manager_module, "_SESSION_CHECK_S", 0.01)
    clock = _Clock(_at(13, 44, 59))
    mgr, quote_rt = _manager(clock)
    switched = threading.Event()

    def _on_subscribe(codes: list[str], after_hours: bool = False) -> None:
        if after_hours:
            switched.set()

    quote_rt.subscribe.side_effect = _on_subscribe
    mgr.subscribe_basket(_cb)

    clock.now = _at(13, 45, 1)

    assert switched.wait(timeout=5.0), "session loop did not switch the flag"
    quote_rt.unsubscribe.assert_called_once_with(["TXFL6"])


def test_close_stops_session_switching() -> None:
    clock = _Clock(_at(13, 44, 59))
    mgr, quote_rt = _manager(clock)
    mgr.subscribe_basket(_cb)

    mgr.close()
    clock.now = _at(13, 46)

    assert mgr.sync_session() is False
    quote_rt.unsubscribe.assert_not_called()
    assert quote_rt.subscribe.call_count == 1


def test_resubscribe_uses_flag_for_current_session() -> None:
    clock = _Clock(_at(10, 0))
    mgr, quote_rt = _manager(clock)
    mgr.subscribe_basket(_cb)

    clock.now = _at(16, 0)

    assert mgr.resubscribe() is True
    quote_rt.stop.assert_called_once()
    assert quote_rt.subscribe.call_args == call(["TXFL6"], after_hours=True)


def test_resubscribe_after_close_subscribes_nothing() -> None:
    clock = _Clock(_at(10, 0))
    mgr, quote_rt = _manager(clock)
    mgr.subscribe_basket(_cb)
    mgr.close()

    assert mgr.resubscribe() is False
    quote_rt.stop.assert_not_called()
    assert quote_rt.subscribe.call_count == 1
