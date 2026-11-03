"""One definition of the TAIFEX front month (``core/front_month.py``)."""

from __future__ import annotations

import datetime as dt

import pytest

from hft_platform.config._symbols_types import ContractIndex, contract_dte_days
from hft_platform.core.front_month import (
    FrontMonth,
    expected_front_delivery,
    front_month,
    front_month_disagreement,
)
from hft_platform.core.market_calendar import taifex_monthly_delivery_date, taifex_third_wednesday

TAIPEI = dt.timezone(dt.timedelta(hours=8))


def _ns(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> int:
    return int(dt.datetime(y, m, d, hh, mm, ss, tzinfo=TAIPEI).timestamp() * 1e9)


class _Calendar:
    """Weekdays trade, except the listed holidays."""

    def __init__(self, holidays: set[dt.date] | None = None) -> None:
        self.holidays = set(holidays or ())

    def is_trading_day(self, date: dt.date) -> bool:
        return date.weekday() < 5 and date not in self.holidays


class _BrokenCalendar:
    def is_trading_day(self, date: dt.date) -> bool:
        raise RuntimeError("calendar out of bounds")


TMF_OCT_NOV_DEC = [
    ("TMFJ6", dt.date(2026, 10, 21)),
    ("TMFK6", dt.date(2026, 11, 18)),
    ("TMFL6", dt.date(2026, 12, 16)),
]


def test_front_month_is_the_settling_contract_until_1330_on_delivery_day() -> None:
    assert front_month(TMF_OCT_NOV_DEC, now_ns=_ns(2026, 10, 21, 13, 29, 59)) == FrontMonth(
        "TMFJ6", dt.date(2026, 10, 21)
    )


def test_front_month_moves_at_1330_on_delivery_day_not_at_midnight() -> None:
    assert front_month(TMF_OCT_NOV_DEC, now_ns=_ns(2026, 10, 21, 13, 30)) == FrontMonth("TMFK6", dt.date(2026, 11, 18))


def test_front_month_tie_on_delivery_date_is_stable() -> None:
    """``TMFR1`` shares its front contract's delivery date."""
    pairs = [("TMFR1", dt.date(2026, 11, 18)), ("TMFK6", dt.date(2026, 11, 18))]
    assert front_month(pairs, now_ns=_ns(2026, 11, 3, 9)).code == "TMFK6"


def test_front_month_skips_undated_rows_and_returns_none_when_nothing_trades() -> None:
    assert front_month([("TMFX", None), ("TMFJ6", dt.date(2026, 10, 21))], now_ns=_ns(2026, 11, 3)) is None


def test_monthly_delivery_moves_past_a_holiday_wednesday() -> None:
    """2026-02-18 was a Lunar New Year closure."""
    cal = _Calendar({dt.date(2026, 2, d) for d in range(16, 21)})
    assert taifex_third_wednesday(2026, 2) == dt.date(2026, 2, 18)
    assert taifex_monthly_delivery_date(2026, 2, calendar=cal) == dt.date(2026, 2, 23)


def test_monthly_delivery_with_the_real_calendar_moves_february_2026() -> None:
    assert taifex_monthly_delivery_date(2026, 2) > dt.date(2026, 2, 18)
    assert taifex_monthly_delivery_date(2026, 11) == dt.date(2026, 11, 18)


def test_monthly_delivery_falls_back_to_the_wednesday_when_the_calendar_cannot_answer() -> None:
    assert taifex_monthly_delivery_date(2026, 2, calendar=_BrokenCalendar()) == dt.date(2026, 2, 18)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        pytest.param(_ns(2026, 11, 3, 9), dt.date(2026, 11, 18), id="early-month"),
        pytest.param(_ns(2026, 11, 18, 13, 29), dt.date(2026, 11, 18), id="delivery-morning"),
        pytest.param(_ns(2026, 11, 18, 13, 30), dt.date(2026, 12, 16), id="delivery-1330"),
        pytest.param(_ns(2026, 12, 20, 9), dt.date(2027, 1, 20), id="year-wrap"),
    ],
)
def test_expected_front_delivery_follows_the_exchange_rule(now: int, expected: dt.date) -> None:
    assert expected_front_delivery(now_ns=now, calendar=_Calendar()) == expected


def test_disagreement_is_none_when_broker_and_rule_agree() -> None:
    assert front_month_disagreement("TMF", TMF_OCT_NOV_DEC, now_ns=_ns(2026, 11, 3, 9), calendar=_Calendar()) is None


def test_disagreement_reports_a_broker_list_missing_the_front_month() -> None:
    reason = front_month_disagreement("TMF", TMF_OCT_NOV_DEC[2:], now_ns=_ns(2026, 11, 3, 9), calendar=_Calendar())
    assert reason is not None
    assert "TMFL6" in reason and "2026-11-18" in reason


def test_disagreement_reports_a_root_with_nothing_trading() -> None:
    reason = front_month_disagreement("TXF", [], now_ns=_ns(2026, 11, 3, 9), calendar=_Calendar())
    assert reason is not None and reason.startswith("TXF: no contract still trading")


def test_builder_dte_uses_the_delivery_cutoff_when_given() -> None:
    """Delivery day after 13:30: the cutoff is tomorrow, so today's contract reads -1."""
    row = {"code": "TMFJ6", "delivery_date": "2026/10/21"}
    assert contract_dte_days(row, dt.date(2026, 10, 22)) == -1
    assert contract_dte_days(row, dt.date(2026, 10, 21)) == 0


def test_contract_index_as_of_defaults_to_none() -> None:
    assert ContractIndex(contracts=[]).as_of is None
