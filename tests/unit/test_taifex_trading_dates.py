from __future__ import annotations

from datetime import datetime

import numpy as np

from research.combinatorial.taifex_trading_dates import (
    BarAggregationLayout,
    build_trading_date_window,
    clickhouse_taifex_bucket_timestamp,
    clickhouse_taifex_contract_predicate,
    clickhouse_taifex_session_predicates,
    full_session_eligibility,
    official_trading_date,
    reaggregate_taifex_bar_rows,
)


def test_friday_night_and_saturday_midnight_map_to_monday_session() -> None:
    window = build_trading_date_window("2026-07-24", "2026-07-27")

    assert official_trading_date(datetime(2026, 7, 24, 15, 0), window) == "2026-07-27"
    assert official_trading_date(datetime(2026, 7, 25, 0, 30), window) == "2026-07-27"


def test_holiday_eve_night_maps_to_next_open_xtai_session() -> None:
    window = build_trading_date_window("2026-04-07", "2026-04-08")

    assert official_trading_date(datetime(2026, 4, 2, 15, 0), window) == "2026-04-07"
    assert all(datetime.fromisoformat(value).weekday() < 5 for value in window.expected_trading_dates)


def test_out_of_session_times_do_not_become_trading_bars() -> None:
    window = build_trading_date_window("2026-07-24", "2026-07-27")

    assert official_trading_date(datetime(2026, 7, 24, 7, 45), window) is None
    assert official_trading_date(datetime(2026, 7, 24, 8, 45), window) == "2026-07-24"
    assert official_trading_date(datetime(2026, 7, 24, 13, 45), window) == "2026-07-24"
    assert official_trading_date(datetime(2026, 7, 24, 13, 45, 1), window) is None
    assert official_trading_date(datetime(2026, 7, 24, 14, 45), window) is None
    assert official_trading_date(datetime(2026, 7, 25, 5, 0), window) == "2026-07-27"
    assert official_trading_date(datetime(2026, 7, 25, 5, 0, 1), window) is None


def test_clickhouse_session_contract_excludes_off_hours_and_rebuckets_closing_prints() -> None:
    expression = "event_time"
    day, night = clickhouse_taifex_session_predicates(expression)
    bucket = clickhouse_taifex_bucket_timestamp(expression)

    assert ">= 525" in day
    assert "< 825" in day
    assert "= 825 AND toSecond(event_time) = 0" in day
    assert ">= 900" in night
    assert "< 300" in night
    assert "= 300 AND toSecond(event_time) = 0" in night
    assert "IN (825, 300)" in bucket
    assert "subtractSeconds(event_time, 1)" in bucket


def test_clickhouse_contract_predicate_is_exact_and_primary_key_indexable() -> None:
    predicate = clickhouse_taifex_contract_predicate()

    assert predicate.startswith("symbol IN (")
    assert "'TXFA0'" in predicate
    assert "'TXFL9'" in predicate
    assert "'TMFA0'" in predicate
    assert "'TMFL9'" in predicate
    assert "match(" not in predicate


def test_coarser_bar_reaggregation_preserves_causal_ohlc_and_additive_fields() -> None:
    hour_ns = 3_600_000_000_000
    origin_ns = int(datetime(2026, 7, 1, 15, 0).timestamp() * 1_000_000_000)
    rows = [
        ("TXF", "TXFG6", "2026-07-02", "night", origin_ns, 100.0, 103.0, 99.0, 102.0, 10, 99.5, 102.5),
        (
            "TXF",
            "TXFG6",
            "2026-07-02",
            "night",
            origin_ns + hour_ns,
            102.0,
            105.0,
            101.0,
            104.0,
            20,
            101.5,
            104.5,
        ),
    ]
    layout = BarAggregationLayout(
        open_index=5,
        high_index=6,
        low_index=7,
        close_index=8,
        sum_indices=(9,),
        first_indices=(10,),
        last_indices=(11,),
    )

    result = reaggregate_taifex_bar_rows(
        rows,
        source_timeframe_min=60,
        target_timeframe_min=120,
        layout=layout,
    )

    assert len(result) == 1
    assert result[0][5:12] == (100.0, 105.0, 99.0, 104.0, 30, 99.5, 104.5)


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
