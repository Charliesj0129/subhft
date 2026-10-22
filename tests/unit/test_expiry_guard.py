"""On a monthly future's delivery date, from the cutoff, the platform stops adding to it.

THESHOW 2026-10-21: R47 carried TMFJ6 +1 into the 13:30 settlement, rebound to TMFK6
and opened another lot beside it, and the settled lot was dropped unbooked at the
next restart. ``ExpiringContractValidator`` refuses openers from the cutoff;
``risk/loss_exit.py`` closes the rest.
"""

from __future__ import annotations

import datetime as dt
from unittest import mock

import pytest

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.risk.expiry import (
    expiry_cutoff_passed,
    expiry_window_ns,
    monthly_delivery_date,
    parse_cutoff_minute,
    taipei_date_and_minute,
)
from hft_platform.risk.validators import ExpiringContractValidator

TPE = dt.timezone(dt.timedelta(hours=8))


def _tpe(y: int, mo: int, d: int, h: int, mi: int) -> int:
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=TPE).timestamp()) * 1_000_000_000


def _intent(symbol: str = "TMFJ6", side: Side = Side.BUY, kind: IntentType = IntentType.NEW) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="R47",
        symbol=symbol,
        intent_type=kind,
        side=side,
        price=467_000_000,
        qty=1,
    )


def _validator(position: int = 0, **guard: object) -> ExpiringContractValidator:
    cfg = {"expiry_guard": guard} if guard else {}
    return ExpiringContractValidator(cfg, None, position_provider=lambda _sym, _sid: position)


def test_taipei_date_and_minute_reads_the_exchange_clock():
    assert taipei_date_and_minute(_tpe(2026, 10, 21, 13, 5)) == (dt.date(2026, 10, 21), 13 * 60 + 5)
    assert taipei_date_and_minute(_tpe(2026, 10, 22, 0, 30)) == (dt.date(2026, 10, 22), 30)


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("TMFJ6", dt.date(2026, 10, 21)),
        ("TMFK6", dt.date(2026, 11, 18)),
        ("TXFL6", dt.date(2026, 12, 16)),
        ("2330", None),
        ("TXO45550J6", None),
        ("TMFR1", None),
    ],
)
def test_monthly_delivery_date_is_the_third_wednesday_for_futures_only(symbol, expected):
    assert monthly_delivery_date(symbol, base_year=2026) == expected


@pytest.mark.parametrize(
    ("raw", "minute"),
    [("13:00", 780), ("12:45", 765), (" 9:05 ", 545), ("25:00", 780), ("13", 780), (None, 780), ("ab:cd", 780)],
)
def test_parse_cutoff_minute_falls_back_on_anything_unreadable(raw, minute):
    assert parse_cutoff_minute(raw) == minute


def test_the_expiry_window_is_the_delivery_date_from_the_cutoff_only():
    start, end = expiry_window_ns("TMFJ6", _tpe(2026, 10, 1, 9, 0), 780)

    assert start == _tpe(2026, 10, 21, 13, 0)
    assert end == _tpe(2026, 10, 22, 0, 0)
    assert not expiry_cutoff_passed("TMFJ6", _tpe(2026, 10, 21, 12, 59), 780)
    assert expiry_cutoff_passed("TMFJ6", _tpe(2026, 10, 21, 13, 0), 780)
    # Settled contracts are not in the window: there is no market to act in.
    assert not expiry_cutoff_passed("TMFJ6", _tpe(2026, 10, 22, 9, 0), 780)
    assert not expiry_cutoff_passed("TMFK6", _tpe(2026, 10, 21, 13, 10), 780)


@pytest.mark.parametrize(
    ("hhmm", "allowed"),
    [((12, 59), True), ((13, 0), False), ((13, 29), False)],
)
def test_an_opener_on_the_delivery_date_is_refused_from_the_cutoff(hhmm, allowed):
    v = _validator()
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=_tpe(2026, 10, 21, *hhmm)):
        ok, reason = v.check(_intent())

    assert ok is allowed
    if not allowed:
        assert reason.startswith("EXPIRY_CUTOFF")


def test_a_reducing_order_and_the_safety_intents_still_pass():
    long_one = _validator(position=1)
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=_tpe(2026, 10, 21, 13, 10)):
        assert long_one.check(_intent(side=Side.SELL)) == (True, "REDUCE_ONLY_BYPASS")
        assert long_one.check(_intent(side=Side.BUY))[0] is False  # adding to it
        assert long_one.check(_intent(kind=IntentType.FORCE_FLAT)) == (True, "OK")
        assert long_one.check(_intent(kind=IntentType.CANCEL)) == (True, "OK")


def test_the_next_month_and_non_futures_are_untouched():
    v = _validator()
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=_tpe(2026, 10, 21, 13, 10)):
        assert v.check(_intent(symbol="TMFK6"))[0] is True
        assert v.check(_intent(symbol="2330"))[0] is True


def test_the_cutoff_and_the_switch_come_from_config():
    later = _validator(cutoff="13:20")
    off = _validator(enabled=False)
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=_tpe(2026, 10, 21, 13, 10)):
        assert later.check(_intent())[0] is True
        assert off.check(_intent())[0] is True
    assert later.cutoff_minute == 13 * 60 + 20
    assert off.enabled is False


def test_a_config_reload_recomputes_the_cached_window():
    v = _validator()
    now = _tpe(2026, 10, 21, 13, 10)
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=now):
        assert v.check(_intent())[0] is False
        v.reload({"expiry_guard": {"cutoff": "13:20"}})
        assert v.check(_intent())[0] is True
