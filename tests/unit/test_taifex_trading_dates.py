from __future__ import annotations

from datetime import datetime

import numpy as np

from research.combinatorial.taifex_trading_dates import (
    build_trading_date_window,
    full_session_eligibility,
    official_trading_date,
)


def test_friday_night_and_saturday_midnight_map_to_monday_session() -> None:
    window = build_trading_date_window("2026-07-24", "2026-07-27")

    assert official_trading_date(datetime(2026, 7, 24, 15, 0), window) == "2026-07-27"
    assert official_trading_date(datetime(2026, 7, 25, 0, 30), window) == "2026-07-27"


def test_holiday_eve_night_maps_to_next_open_xtai_session() -> None:
    window = build_trading_date_window("2026-04-07", "2026-04-08")

    assert official_trading_date(datetime(2026, 4, 2, 15, 0), window) == "2026-04-07"
    assert all(datetime.fromisoformat(value).weekday() < 5 for value in window.expected_trading_dates)


def test_full_session_eligibility_requires_both_roots_and_exposes_partial_counts() -> None:
    roots: list[str] = []
    days: list[str] = []
    sessions: list[str] = []
    for root in ("TXF", "TMF"):
        for session, count in (("night", 14), ("day", 5)):
            roots.extend([root] * count)
            days.extend(["2026-07-24"] * count)
            sessions.extend([session] * count)
    roots.pop()
    days.pop()
    sessions.pop()

    evidence = full_session_eligibility(
        root=np.asarray(roots),
        timeframe_min=np.full(len(roots), 60),
        trading_day=np.asarray(days),
        session=np.asarray(sessions),
        expected_trading_dates=("2026-07-24",),
        roots=("TXF", "TMF"),
    )

    assert evidence.eligible_trading_dates == ()
    assert evidence.excluded_partial_trading_dates[0]["roots"][1]["day_60m_bars"] == 4


def test_full_session_eligibility_excludes_a_day_missing_from_a_requested_timeframe() -> None:
    roots: list[str] = []
    timeframes: list[int] = []
    days: list[str] = []
    sessions: list[str] = []
    expected_days = ("2026-07-23", "2026-07-24")
    for day in expected_days:
        for root in ("TXF", "TMF"):
            for session, count in (("night", 14), ("day", 5)):
                roots.extend([root] * count)
                timeframes.extend([60] * count)
                days.extend([day] * count)
                sessions.extend([session] * count)
            if not (day == "2026-07-24" and root == "TMF"):
                roots.append(root)
                timeframes.append(1440)
                days.append(day)
                sessions.append("full")

    evidence = full_session_eligibility(
        root=np.asarray(roots),
        timeframe_min=np.asarray(timeframes),
        trading_day=np.asarray(days),
        session=np.asarray(sessions),
        expected_trading_dates=expected_days,
        roots=("TXF", "TMF"),
        required_timeframes=(60, 1440),
    )

    assert evidence.eligible_trading_dates == ("2026-07-23",)
    exclusion = evidence.excluded_partial_trading_dates[0]
    assert exclusion["trading_date"] == "2026-07-24"
    assert exclusion["reason"] == "incomplete_cross_timeframe_coverage"
    assert exclusion["root_timeframe_groups"][-1] == {
        "root": "TMF",
        "timeframe_min": 1440,
        "bar_count": 0,
        "present": False,
    }
