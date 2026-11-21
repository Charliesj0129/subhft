"""Scaling, time conversion, session classification, calendar guards and the rate limiter of the hist ingester."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from research.data_pipeline.hist_ingest import model
from research.data_pipeline.hist_ingest.ratelimit import RateLimiter

NS = 1_000_000_000
MIGRATION = Path("src/hft_platform/migrations/clickhouse/20261121_001_create_hist_market_data.sql")


def taipei(day: str, hhmm: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"{day}T{hhmm}:00+08:00")


def utc_ns(moment: dt.datetime) -> int:
    return int(moment.timestamp()) * NS


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (17660.5, 17660500000),
        (0.1 + 0.2, 300000),
        (23456.25, 23456250000),
        (2.01, 2010000),
        ("100.000001", 100000001),
        (0, 0),
    ],
)
def test_scale_price_is_exact_for_float_noise_and_decimal_strings(value, expected):
    assert model.scale_price(value) == expected
    assert isinstance(model.scale_price(value), int)


def test_scale_price_rejects_nan_instead_of_storing_garbage():
    with pytest.raises(ValueError):
        model.scale_price(float("nan"))


def test_shioaji_local_ns_to_utc_subtracts_eight_hours():
    local_ns = utc_ns(dt.datetime(2026, 11, 20, 9, 0, tzinfo=dt.UTC))  # wall clock 09:00 stored as if it were UTC
    assert model.local_ns_to_utc_ns(local_ns) == utc_ns(taipei("2026-11-20", "09:00"))


def test_iso_to_utc_ns_honours_the_plus_eight_offset():
    assert model.iso_to_utc_ns("2026-11-20T09:00:00.000+08:00") == utc_ns(
        dt.datetime(2026, 11, 20, 1, 0, tzinfo=dt.UTC)
    )
    assert (
        model.iso_to_utc_ns("2026-11-20T01:00:00.500000Z")
        == utc_ns(dt.datetime(2026, 11, 20, 1, 0, tzinfo=dt.UTC)) + NS // 2
    )


def test_iso_to_utc_ns_refuses_a_timestamp_without_offset():
    with pytest.raises(ValueError):
        model.iso_to_utc_ns("2026-11-20T09:00:00")


@pytest.mark.parametrize(
    ("hhmm", "expected"),
    [
        ("08:44", "night"),
        ("08:45", "day"),
        ("13:59", "day"),
        ("14:00", "night"),
        ("00:30", "night"),
        ("21:00", "night"),
    ],
)
def test_classify_session_at_the_boundaries(hhmm, expected):
    assert model.classify_session(utc_ns(taipei("2026-11-20", hhmm))) == expected


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        ("2026-11-20T15:30", dt.date(2026, 11, 20)),
        ("2026-11-20T10:00", dt.date(2026, 11, 19)),
        ("2026-11-23T09:00", dt.date(2026, 11, 20)),
        ("2026-11-21T20:00", dt.date(2026, 11, 20)),
    ],
)
def test_last_completed_trading_day_skips_open_sessions_and_weekends(moment, expected):
    assert model.last_completed_trading_day(taipei(moment[:10], moment[11:])) == expected


def test_intraday_tick_query_is_refused_at_ten_on_a_weekday():
    with pytest.raises(model.IntradayRefusedError):
        model.intraday_tick_cap(taipei("2026-11-20", "10:00"), False, 20)


def test_intraday_tick_query_is_capped_at_eight_when_allowed_and_free_at_1530():
    assert model.intraday_tick_cap(taipei("2026-11-20", "10:00"), True, 20) == 8
    assert model.intraday_tick_cap(taipei("2026-11-20", "15:30"), False, 20) == 20
    assert model.intraday_tick_cap(taipei("2026-11-21", "10:00"), False, 20) == 20  # Saturday


def test_rate_limiter_waits_for_the_window_without_a_real_sleep():
    clock = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    limiter = RateLimiter(2, 10.0, clock=lambda: clock[0], sleep=sleep)
    for _ in range(3):
        limiter.acquire()
    assert slept == [10.0]
    assert clock[0] == 10.0


@pytest.mark.parametrize(
    ("table", "row"),
    [("hist_ticks", model.TickRow), ("hist_kbars", model.KbarRow), ("hist_ingest_watermark", model.WatermarkRow)],
)
def test_row_fields_match_the_migration_columns_in_order(table, row):
    body = re.search(rf"hft\.{table} \((.*?)\) ENGINE", MIGRATION.read_text(), re.S).group(1)
    assert [line.split()[0] for line in body.strip().splitlines()] == list(row._fields)
