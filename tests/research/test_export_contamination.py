"""Regression contracts for source defects the L2 exporter must not propagate.

Coverage gate is bypassed for this tree; invoke explicitly, e.g.:
    uv run pytest tests/research/test_export_contamination.py --no-cov -q

The shapes exercised here are not hypothetical. They are the defects the source audit
measured over the real 936M-row corpus (report ``9606665855875e86…``):

* **2026-04-03** -- an XTAI-closed date on which the Tick channel kept publishing real
  trades from 06:00 to 12:59 Taipei while the BidAsk channel correctly stopped at 05:00.
  198,678 rows, ``exch_ts == ingest_ts``, so the causality check structurally cannot see
  them. The night-session tail on that same date (00:00-05:00, belonging to trading day
  2026-04-02) is legitimate and must survive.
* **3,189,310 empty or one-sided BidAsk rows** across the corpus.
* Non-positive trade prices and zero-volume ticks.

The last two were already excluded by the exporter; those tests pin existing behaviour
so it cannot regress silently.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from research import data_pipeline
from research.data_pipeline import (
    TAIPEI_UTC_OFFSET_NS,
    filter_session_rows,
    quality,
    rows_to_l2_and_ticks,
)

TAIPEI = ZoneInfo("Asia/Taipei")

# 2026-04-02 was an XTAI session; 2026-04-03 and 2026-04-06 were not.
SESSIONS = frozenset({"2026-04-01", "2026-04-02", "2026-04-07"})


def _ts(day: str, hour: int, minute: int, second: int = 0) -> int:
    """Taipei wall-clock to nanoseconds since epoch."""
    moment = datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:{second:02d}").replace(tzinfo=TAIPEI)
    return int(moment.timestamp()) * 1_000_000_000


def _bidask(ts: int, bid: int = 32_800_000_000, ask: int = 32_801_000_000) -> tuple[object, ...]:
    return ("BidAsk", ts, ts, [bid], [ask], [10], [12], 0, 0)


def _tick(ts: int, price: int = 32_800_500_000, volume: int = 3) -> tuple[object, ...]:
    return ("Tick", ts, ts, [], [], [], [], price, volume)


def _april_third_shape() -> list[tuple[object, ...]]:
    """The real 2026-04-03 shape: a legitimate night tail, then out-of-session ticks."""
    rows: list[tuple[object, ...]] = []
    # Night session of trading day 2026-04-02, running past midnight to 05:00.
    for minute in (0, 30, 240, 299):
        rows.append(_bidask(_ts("2026-04-03", minute // 60, minute % 60)))
        rows.append(_tick(_ts("2026-04-03", minute // 60, minute % 60)))
    # Contamination: ticks after the night close, on a date the exchange was shut.
    # 06:00-08:44 falls between the session windows; 08:45-12:59 falls inside the day
    # window but on a non-session date. Both must go.
    for hour, minute in ((6, 0), (7, 30), (8, 44), (8, 45), (10, 0), (12, 59)):
        rows.append(_tick(_ts("2026-04-03", hour, minute)))
    return rows


# ---------------------------------------------------------------------------
# Session filtering -- the defect this change closes
# ---------------------------------------------------------------------------


def test_out_of_session_ticks_are_excluded_from_l2_export() -> None:
    rows = _april_third_shape()
    kept, dropped = filter_session_rows(rows, SESSIONS)

    assert dropped == 6, "all six post-05:00 ticks on a closed date must be dropped"
    assert len(kept) == 8, "the four night-session book/tick pairs must survive"

    events, ticks, _ = rows_to_l2_and_ticks(kept)
    assert len(ticks) == 4
    latest_kept = max(int(row[1]) for row in kept)
    assert latest_kept <= _ts("2026-04-03", 5, 0), "nothing after the 05:00 night close may remain"


def test_night_session_tail_is_kept_when_previous_day_is_a_session() -> None:
    """00:00-05:00 belongs to the night session that opened at 15:00 the day before."""
    rows = [_bidask(_ts("2026-04-03", 2, 0)), _tick(_ts("2026-04-03", 4, 59))]
    kept, dropped = filter_session_rows(rows, SESSIONS)
    assert dropped == 0
    assert len(kept) == 2


def test_night_session_open_is_kept_on_the_eve_of_a_holiday() -> None:
    """The 15:00 open depends on the *current* date being a session, not the next one."""
    rows = [_bidask(_ts("2026-04-02", 15, 0)), _tick(_ts("2026-04-02", 23, 59))]
    kept, dropped = filter_session_rows(rows, SESSIONS)
    assert dropped == 0
    assert len(kept) == 2


def test_day_session_rows_are_dropped_on_a_non_session_date() -> None:
    """08:45-13:45 is a valid clock window, so only the calendar can reject these."""
    rows = [_tick(_ts("2026-04-03", 9, 0)), _tick(_ts("2026-04-03", 12, 59))]
    kept, dropped = filter_session_rows(rows, SESSIONS)
    assert kept == []
    assert dropped == 2


def test_rows_between_sessions_are_dropped_regardless_of_calendar() -> None:
    """05:00-08:44 is outside every window, so it goes even on a real session date."""
    rows = [_tick(_ts("2026-04-02", 6, 0)), _tick(_ts("2026-04-02", 8, 44))]
    kept, dropped = filter_session_rows(rows, SESSIONS)
    assert kept == []
    assert dropped == 2


def test_session_filter_falls_back_to_window_only_without_a_calendar() -> None:
    """Without a calendar the clock rule still applies; the date rule cannot."""
    rows = _april_third_shape()
    kept, dropped = filter_session_rows(rows, None)

    assert dropped == 3, "06:00, 07:30 and 08:44 are outside every window"
    kept_ts = {int(row[1]) for row in kept}
    assert _ts("2026-04-03", 10, 0) in kept_ts, "a calendar is required to reject a day-window row"


def test_taipei_offset_matches_zoneinfo_for_a_corpus_date() -> None:
    """Pins the fixed-offset assumption the row filter relies on for speed."""
    for day in ("2026-01-26", "2026-06-15", "2026-08-06"):
        moment = datetime.fromisoformat(f"{day}T12:00:00").replace(tzinfo=TAIPEI)
        offset = moment.utcoffset()
        assert offset is not None
        assert int(offset.total_seconds()) * 1_000_000_000 == TAIPEI_UTC_OFFSET_NS


# ---------------------------------------------------------------------------
# Existing defences -- pinned so they cannot regress
# ---------------------------------------------------------------------------


def test_empty_and_one_sided_books_produce_no_depth_events() -> None:
    ts = _ts("2026-04-02", 9, 0)
    rows: list[tuple[object, ...]] = [
        ("BidAsk", ts, ts, [], [], [], [], 0, 0),
        ("BidAsk", ts + 1, ts + 1, [32_800_000_000], [], [10], [], 0, 0),
        ("BidAsk", ts + 2, ts + 2, [], [32_801_000_000], [], [12], 0, 0),
    ]
    events, ticks, _ = rows_to_l2_and_ticks(rows)
    assert len(events) == 0
    assert len(ticks) == 0


def test_zero_price_or_zero_volume_ticks_are_dropped() -> None:
    ts = _ts("2026-04-02", 9, 0)
    rows: list[tuple[object, ...]] = [
        _bidask(ts),
        _tick(ts + 1, price=0, volume=5),
        _tick(ts + 2, price=32_800_000_000, volume=0),
        _tick(ts + 3, price=32_800_000_000, volume=7),
    ]
    _, ticks, _ = rows_to_l2_and_ticks(rows)
    assert len(ticks) == 1
    assert float(ticks[0]["qty"]) == 7.0


# ---------------------------------------------------------------------------
# Export wiring: the summary and sidecar must state what the session rule removed
# ---------------------------------------------------------------------------


def _stub_export(monkeypatch: Any, rows: list[tuple[object, ...]]) -> None:
    monkeypatch.setattr(data_pipeline, "_get_client", lambda *a, **k: object())
    monkeypatch.setattr(data_pipeline, "_discover_symbol_dates", lambda *a, **k: [("2026-04-03", len(rows))])
    monkeypatch.setattr(data_pipeline, "_fetch_day_rows", lambda *a, **k: rows)
    monkeypatch.setattr(data_pipeline.quality, "load_latest_report", lambda *a, **k: None)
    monkeypatch.setattr(data_pipeline.quality, "expected_trading_days", lambda *a, **k: sorted(SESSIONS))


def _export(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    return data_pipeline.export_l2_ticks(
        symbols=["TXFD6"],
        date_from="2026-04-03",
        date_to="2026-04-03",
        host="localhost",
        port=8123,
        user="default",
        password="",
        out_dir=tmp_path,
        **kwargs,
    )


def test_export_records_the_session_rule_and_filtered_count_in_the_sidecar(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _stub_export(monkeypatch, _april_third_shape())
    summary = _export(tmp_path)

    assert summary["session_rule"] == data_pipeline.SESSION_RULE_CALENDAR
    assert summary["errors"] == []
    output = summary["outputs"][0]
    assert output["status"] == "exported"
    assert output["session_filtered_rows"] == 6
    assert output["source_rows"] == 14, "the discovery count stays pre-filter and is reported as such"

    meta = json.loads((tmp_path / "txfd6" / "TXFD6_2026-04-03_ticks.npy.meta.json").read_text())
    assert meta["session_filtered_rows"] == 6
    assert meta["session_rule"] == data_pipeline.SESSION_RULE_CALENDAR
    assert meta["source_quality_verdict"] == "unstamped"


def test_export_skips_a_date_whose_rows_are_all_out_of_session(monkeypatch: Any, tmp_path: Path) -> None:
    """A fully contaminated date is a skip, not an error -- so real failures stay legible."""
    contaminated = [_tick(_ts("2026-04-03", hour, 0)) for hour in (6, 9, 12)]
    _stub_export(monkeypatch, contaminated)
    summary = _export(tmp_path)

    assert summary["errors"] == []
    assert summary["outputs"] == [
        {
            "symbol": "TXFD6",
            "date": "2026-04-03",
            "status": "skipped_non_session",
            "session_filtered_rows": 3,
        }
    ]
    assert not (tmp_path / "txfd6").exists()


def test_allow_non_session_relaxes_only_the_calendar_half(monkeypatch: Any, tmp_path: Path) -> None:
    _stub_export(monkeypatch, _april_third_shape())
    summary = _export(tmp_path, allow_non_session=True)

    assert summary["session_rule"] == data_pipeline.SESSION_RULE_WINDOW_ONLY
    assert summary["outputs"][0]["session_filtered_rows"] == 3, "the clock windows still apply"


# ---------------------------------------------------------------------------
# Archive sync check
# ---------------------------------------------------------------------------


def _inventory(*entries: tuple[str, int, str | None]) -> dict[str, object]:
    return {
        "schema": "hft_archive_inventory.v1",
        "generated_at": "2026-08-07T00:00:00+00:00",
        "partitions": [
            {"partition": name, "rows": rows, "ttl_expiry": ttl} for name, rows, ttl in entries
        ],
    }


def test_archive_sync_reports_error_when_missing_partition_expires_soon() -> None:
    now = datetime(2026, 8, 7, tzinfo=UTC)
    soon = (now + timedelta(days=5)).isoformat()
    result = quality.evaluate_archive_sync(
        {"20260805": 100},
        _inventory(("20260805", 100, None), ("20260806", 200, soon)),
        now=now,
    )
    assert result.status == "fail"
    assert result.severity == "error"
    assert result.detail["urgent_partitions"] == ["20260806"]


def test_archive_sync_reports_warn_when_missing_partition_has_runway() -> None:
    now = datetime(2026, 8, 7, tzinfo=UTC)
    far = (now + timedelta(days=180)).isoformat()
    result = quality.evaluate_archive_sync(
        {"20260805": 100},
        _inventory(("20260805", 100, None), ("20260806", 200, far)),
        now=now,
    )
    assert result.status == "fail"
    assert result.severity == "warn"
    assert result.detail["urgent_partitions"] == []
    assert result.detail["days_until_permanent_loss"] == 180


def test_archive_sync_flags_row_count_delta_without_calling_it_missing() -> None:
    now = datetime(2026, 8, 7, tzinfo=UTC)
    result = quality.evaluate_archive_sync(
        {"20260804": 7_338_496},
        _inventory(("20260804", 7_338_498, None)),
        now=now,
    )
    assert result.detail["missing_partitions"] == []
    assert result.detail["row_delta_partitions"] == [
        {"partition": "20260804", "local_rows": 7_338_496, "remote_rows": 7_338_498}
    ]


def test_archive_sync_reports_unavailable_without_reference_inventory() -> None:
    result = quality.evaluate_archive_sync({"20260805": 100}, None)
    assert result.status == "unavailable"
    assert "sync_market_data_archive.py" in result.detail["producer"]


def test_archive_sync_passes_when_archive_matches_reference() -> None:
    result = quality.evaluate_archive_sync(
        {"20260805": 100, "20260806": 200},
        _inventory(("20260805", 100, None), ("20260806", 200, None)),
    )
    assert result.status == "pass"


def test_reference_inventory_round_trips_through_disk(tmp_path: Path) -> None:
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(_inventory(("20260805", 100, None))), encoding="utf-8")
    loaded = quality.load_reference_inventory(path)
    assert loaded is not None
    assert loaded["partitions"][0]["partition"] == "20260805"
    assert quality.load_reference_inventory(tmp_path / "absent.json") is None
