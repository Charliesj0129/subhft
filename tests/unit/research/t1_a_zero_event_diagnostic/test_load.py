from __future__ import annotations

from pathlib import Path

import pytest

from research.tools.t1_a_zero_event_diagnostic.load import (
    csv_sha256,
    freshness_check,
    load_and_dedupe_coverage,
    read_viability_event_count,
)
from tests.unit.research.t1_a_zero_event_diagnostic.conftest import coverage_row


def test_csv_sha256_stable(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    assert csv_sha256(p) == csv_sha256(p)
    assert len(csv_sha256(p)) == 64


def test_load_and_dedupe_keeps_later_bbo_last_time(make_coverage_csv):
    rows_a = [
        coverage_row(
            contract="TXFD6",
            trading_day="2026-04-01",
            bbo_last_time="2026-04-01T05:00:00+00:00",
            or_high=100.0,
        )
    ]
    rows_b = [
        coverage_row(
            contract="TXFD6",
            trading_day="2026-04-01",
            bbo_last_time="2026-04-01T05:45:00+00:00",
            or_high=999.0,
        )
    ]
    a = make_coverage_csv(rows_a, name="a.csv")
    b = make_coverage_csv(rows_b, name="b.csv")
    df, sha_map = load_and_dedupe_coverage([a, b])
    assert len(df) == 1
    assert df.iloc[0]["or_high"] == 999.0
    assert sha_map[str(a)] == csv_sha256(a)
    assert sha_map[str(b)] == csv_sha256(b)


def test_load_and_dedupe_tie_breaker_on_missing_bbo_last_time(make_coverage_csv):
    rows_a = [
        coverage_row(
            contract="TXFD6",
            trading_day="2026-04-01",
            bbo_last_time=None,
            or_high=111.0,
        )
    ]
    rows_b = [
        coverage_row(
            contract="TXFD6",
            trading_day="2026-04-01",
            bbo_last_time=None,
            or_high=222.0,
        )
    ]
    a = make_coverage_csv(rows_a, name="a.csv")
    b = make_coverage_csv(rows_b, name="b.csv")
    df, _ = load_and_dedupe_coverage([a, b])
    assert len(df) == 1
    assert df.iloc[0]["or_high"] == 222.0


def test_load_records_sha256_per_path(make_coverage_csv):
    a = make_coverage_csv([coverage_row()], name="a.csv")
    b = make_coverage_csv([coverage_row(trading_day="2026-04-02")], name="b.csv")
    _, sha_map = load_and_dedupe_coverage([a, b])
    assert sha_map[str(a)] != sha_map[str(b)]


def test_load_empty_input_raises():
    with pytest.raises(ValueError, match="no coverage rows"):
        load_and_dedupe_coverage([])


def test_load_all_empty_files_raises(make_coverage_csv):
    a = make_coverage_csv([], name="empty.csv")
    with pytest.raises(ValueError, match="no coverage rows"):
        load_and_dedupe_coverage([a])


def test_read_viability_event_count_with_empty_file(viability_event_csv):
    p = viability_event_csv(n_events=0)
    assert read_viability_event_count(p) == 0


def test_read_viability_event_count_with_n_events(viability_event_csv):
    p = viability_event_csv(n_events=4)
    assert read_viability_event_count(p) == 4


def test_read_viability_event_count_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        read_viability_event_count(tmp_path / "missing.csv")


def test_freshness_check_matches_summary(tmp_path: Path, make_coverage_csv):
    coverage = make_coverage_csv(
        [coverage_row(trading_day="2026-04-01"), coverage_row(trading_day="2026-04-02")]
    )
    df, _ = load_and_dedupe_coverage([coverage])
    events = tmp_path / "20260513T153706Z_opening_range_events.csv"
    events.write_text("", encoding="utf-8")
    summary = tmp_path / "20260513T153706Z_summary.json"
    summary.write_text('{"audited_trading_days": 2, "events": 0}', encoding="utf-8")
    out = freshness_check(df, events)
    assert out["summary_path"] == str(summary)
    assert out["audited_trading_days_summary"] == 2
    assert out["audited_trading_days_in_input"] == 2
    assert out["match"] is True


def test_freshness_check_uses_unique_trading_days(tmp_path: Path, make_coverage_csv):
    coverage = make_coverage_csv(
        [
            coverage_row(contract="TXFB6", trading_day="2026-04-01"),
            coverage_row(contract="TXFD6", trading_day="2026-04-01"),
        ]
    )
    df, _ = load_and_dedupe_coverage([coverage])
    events = tmp_path / "20260513T153706Z_opening_range_events.csv"
    events.write_text("", encoding="utf-8")
    summary = tmp_path / "20260513T153706Z_summary.json"
    summary.write_text('{"audited_trading_days": 1, "events": 0}', encoding="utf-8")
    out = freshness_check(df, events)
    assert out["audited_trading_days_in_input"] == 1
    assert out["match"] is True
