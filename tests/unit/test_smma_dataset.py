from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

import research.combinatorial.smma_dataset as smma_dataset_module
from research.combinatorial.smma_dataset import (
    DatasetGovernanceError,
    _bar_query,
    _guard_query,
    _metadata_hash,
    export_clickhouse_dataset,
    load_governed_dataset,
    rows_to_bar_dataset,
    save_governed_dataset,
)


def _row(
    root: str,
    contract: str,
    day: str,
    ts: int,
    volume: int,
    *,
    session: str = "day",
) -> tuple[object, ...]:
    price = 100.0 + (ts / 1_000_000_000_000)
    return (
        root,
        contract,
        day,
        session,
        ts,
        price,
        price + 1.0,
        price - 1.0,
        price + 0.2,
        volume,
        price - 0.1,
        price + 0.1,
        10,
        12,
        price - 0.05,
        price + 0.05,
        8,
        9,
    )


def _causal_rows() -> dict[int, list[tuple[object, ...]]]:
    rows: list[tuple[object, ...]] = []
    day_specs = (
        ("2026-07-01", 100, 50),
        ("2026-07-02", 10, 200),
        ("2026-07-03", 20, 300),
        ("2026-07-06", 30, 400),
    )
    for day_index, (day, volume_a, volume_b) in enumerate(day_specs):
        base = (day_index + 1) * 10_000_000_000_000
        for offset in (0, 3_600_000_000_000):
            rows.append(_row("TXF", "TXFA6", day, base + offset, volume_a))
            rows.append(_row("TXF", "TXFB6", day, base + offset, volume_b))
    return {60: rows}


def _complete_export_rows() -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    trading_days = ("2026-06-30", "2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06")
    for day_index, day in enumerate(trading_days):
        for root in ("TXF", "TMF"):
            contract = f"{root}D6"
            base = (day_index + 1) * 100_000_000_000_000
            for offset in range(14):
                rows.append(
                    _row(
                        root,
                        contract,
                        day,
                        base + offset * 3_600_000_000_000,
                        100,
                        session="night",
                    )
                )
            for offset in range(5):
                rows.append(
                    _row(
                        root,
                        contract,
                        day,
                        base + (14 + offset) * 3_600_000_000_000,
                        100,
                        session="day",
                    )
                )
    return rows


class _FakeResult:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.result_rows = rows


class _FakeClient:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows
        self.settings: list[dict[str, Any]] = []
        self.queries: list[str] = []

    def query(self, query: str, settings: dict[str, Any] | None = None) -> _FakeResult:
        self.queries.append(query)
        self.settings.append(dict(settings or {}))
        return _FakeResult(self._rows)


def test_dataset_front_contract_uses_previous_day_volume_and_resets_on_roll() -> None:
    dataset = rows_to_bar_dataset(_causal_rows())
    assert set(dataset.trading_day) == {"2026-07-02", "2026-07-03", "2026-07-06"}
    by_day = {day: set(dataset.contract[dataset.trading_day == day]) for day in set(dataset.trading_day)}
    assert by_day["2026-07-02"] == {"TXFA6"}
    assert by_day["2026-07-03"] == {"TXFB6"}
    assert by_day["2026-07-06"] == {"TXFB6"}
    roll_index = int(np.flatnonzero(dataset.trading_day == "2026-07-03")[0])
    assert dataset.reset[roll_index]


def test_dataset_resets_after_one_missing_intraday_bar() -> None:
    rows = _causal_rows()[60]
    rows = [
        row
        for row in rows
        if not (row[2] == "2026-07-03" and row[1] == "TXFB6" and int(row[4]) == 30_000_000_000_000 + 3_600_000_000_000)
    ]
    rows.append(_row("TXF", "TXFB6", "2026-07-03", 30_000_000_000_000 + 7_200_000_000_000, 300))
    dataset = rows_to_bar_dataset({60: rows})
    day_indices = np.flatnonzero(dataset.trading_day == "2026-07-03")
    assert day_indices.size == 2
    assert dataset.reset[day_indices[-1]]


def test_dataset_aligns_root_groups_to_common_days_and_resets_after_exclusion() -> None:
    day_values = ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06")
    rows: list[tuple[object, ...]] = []
    for day_index, day in enumerate(day_values):
        timestamp = day_index * 86_400_000_000_000
        rows.append(_row("TXF", "TXFA6", day, timestamp, 100))
        if day != "2026-07-03":
            rows.append(_row("TMF", "TMFA6", day, timestamp, 100))

    dataset = rows_to_bar_dataset({60: rows})

    assert set(dataset.trading_day) == {"2026-07-02", "2026-07-06"}
    assert set(dataset.trading_day[dataset.root == "TXF"]) == set(dataset.trading_day[dataset.root == "TMF"])
    txf_last = int(np.flatnonzero(dataset.root == "TXF")[-1])
    assert dataset.reset[txf_last]


def test_dataset_sidecar_detects_content_tamper(tmp_path) -> None:
    dataset = rows_to_bar_dataset(_causal_rows())
    path = tmp_path / "dataset.npz"
    _output, sidecar = save_governed_dataset(
        path,
        dataset,
        query_evidence=[{"query_sha256": "abc", "guard_overall": "pass"}],
        code_fingerprint="code",
    )
    loaded = load_governed_dataset(path)
    assert len(loaded) == len(dataset)
    payload = json.loads(sidecar.read_text())
    assert payload["price_scale_source"] == 1_000_000
    assert payload["roll_rule"].startswith("front contract selected by previous")
    assert np.all(loaded.bid_qty_open == 10)
    assert np.all(loaded.ask_qty_open == 12)
    assert np.all(loaded.bid_qty_close == 8)
    assert np.all(loaded.ask_qty_close == 9)

    with path.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(DatasetGovernanceError, match="fingerprint mismatch"):
        load_governed_dataset(path)


def test_dataset_sidecar_detects_metadata_tamper(tmp_path) -> None:
    dataset = rows_to_bar_dataset(_causal_rows())
    path = tmp_path / "dataset.npz"
    _output, sidecar = save_governed_dataset(
        path,
        dataset,
        query_evidence=[{"query_sha256": "abc", "guard_overall": "pass"}],
        code_fingerprint="code",
    )
    payload = json.loads(sidecar.read_text())
    payload["roll_rule"] = "same-day volume"
    sidecar.write_text(json.dumps(payload))
    with pytest.raises(DatasetGovernanceError, match="sidecar fingerprint mismatch"):
        load_governed_dataset(path)


def test_legacy_v1_dataset_remains_readable_for_status_compatibility(tmp_path) -> None:
    dataset = rows_to_bar_dataset(_causal_rows())
    path = tmp_path / "dataset.npz"
    _output, sidecar = save_governed_dataset(
        path,
        dataset,
        query_evidence=[{"query_sha256": "abc", "guard_overall": "pass"}],
        code_fingerprint="code",
    )
    payload = json.loads(sidecar.read_text())
    payload.pop("metadata_hash")
    payload["schema"] = "smma_taifex_bars.v1"
    payload["schema_version"] = 1
    payload["metadata_hash"] = _metadata_hash(payload)
    sidecar.write_text(json.dumps(payload))

    assert len(load_governed_dataset(path)) == len(dataset)


def test_dataset_query_guard_blocks_mutation() -> None:
    with pytest.raises(DatasetGovernanceError):
        _guard_query("DROP TABLE hft.market_data")


def test_bar_query_casts_datetime_bucket_before_nanosecond_conversion() -> None:
    query = _bar_query(60, date_from="2026-03-19", date_to="2026-07-24")
    assert "toUnixTimestamp64Nano(toDateTime64(bucket, 9, 'Asia/Taipei'))" in query
    assert "toDateTime64('1970-01-01 08:45:00', 9, 'Asia/Taipei')" in query
    assert "toDateTime64('1970-01-01 15:00:00', 9, 'Asia/Taipei')" in query
    assert "< 825" in query
    assert "< 300" in query
    assert "subtractSeconds(" in query
    assert "toHour(fromUnixTimestamp64Nano(exch_ts, 'Asia/Taipei')) < 6" not in query
    assert "PREWHERE symbol IN (" in query
    assert "exch_ts >= toUnixTimestamp64Nano(range_start)" in query
    assert "exch_ts < toUnixTimestamp64Nano(range_end)" in query
    assert "match(symbol" not in query


def test_two_minute_query_is_guarded_and_has_nontruncating_result_cap() -> None:
    query = _bar_query(2, date_from="2026-03-19", date_to="2026-07-24")

    assert "INTERVAL 2 MINUTE" in query
    assert "LIMIT 1000000" in query
    assert _guard_query(query)["guard_overall"] == "pass"


def test_dataset_accepts_two_minute_bars_with_causal_roll() -> None:
    dataset = rows_to_bar_dataset({2: _causal_rows()[60]})

    assert set(dataset.timeframe_min) == {2}
    assert set(dataset.trading_day) == {"2026-07-02", "2026-07-03", "2026-07-06"}


def test_daily_query_aggregates_full_trading_day() -> None:
    query = _bar_query(1440, date_from="2026-03-19", date_to="2026-07-24")
    assert "'full' AS session" in query
    assert "toStartOfDay(toDateTime(trading_day, 'Asia/Taipei')) AS bucket" in query
    assert "INTERVAL 1440 MINUTE" not in query
    assert _guard_query(query)["guard_overall"] == "pass"


def test_bar_export_queries_once_and_records_causal_derivation_evidence(tmp_path) -> None:
    client = _FakeClient(_complete_export_rows())
    path = tmp_path / "dataset.npz"

    _output, sidecar = export_clickhouse_dataset(
        path,
        code_fingerprint="code",
        client=client,
        date_from="2026-07-01",
        date_to="2026-07-06",
        timeframes_minutes=(60, 120, 240, 1440),
    )

    assert len(client.queries) == 1
    assert client.settings == [
        {
            "readonly": 1,
            "max_memory_usage": 2_147_483_648,
            "max_threads": 2,
            "max_execution_time": 300,
            "max_result_rows": 100_000,
            "result_overflow_mode": "throw",
        }
    ]
    payload = json.loads(sidecar.read_text())
    assert [item["timeframe_min"] for item in payload["query_evidence"]] == [60, 120, 240, 1440]
    assert payload["query_evidence"][0]["derived"] is False
    assert all(item["derived_from_timeframe_min"] == 60 for item in payload["query_evidence"][1:])
    assert all(item["derivation"] == "causal_bar_reaggregation.v1" for item in payload["query_evidence"][1:])
    assert payload["eligible_trading_dates"] == [
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
        "2026-07-06",
    ]
    assert set(load_governed_dataset(path).timeframe_min) == {60, 120, 240, 1440}


def _audit_report(date_from: str, date_to: str, *, verdict: str = "pass") -> Any:
    """A minimal QualityReport standing in for a real source-layer audit."""
    from research.data_pipeline.quality import QualityReport

    return QualityReport(
        schema="source_audit.v1",
        generated_at="2026-08-01T00:00:00+00:00",
        source="hft.market_data",
        date_from=date_from,
        date_to=date_to,
        verdict=verdict,
        checks=(),
        extent={"rows": 1},
        report_sha256="a" * 64,
    )


def _save_with_report(tmp_path, monkeypatch, report) -> dict[str, Any]:
    monkeypatch.setattr(smma_dataset_module, "load_latest_report", lambda *_a, **_k: report)
    dataset = rows_to_bar_dataset(_causal_rows())
    path = tmp_path / "dataset.npz"
    _output, sidecar = save_governed_dataset(
        path,
        dataset,
        query_evidence=[{"query_sha256": "abc", "guard_overall": "pass"}],
        code_fingerprint="code",
    )
    return json.loads(sidecar.read_text())


def test_sidecar_records_an_unstamped_verdict_when_no_source_audit_exists(tmp_path, monkeypatch) -> None:
    payload = _save_with_report(tmp_path, monkeypatch, None)

    # The absence of an audit must be visible in the artifact, not inferred from a
    # missing key -- a reader cannot tell "never audited" from "key dropped".
    assert payload["source_quality_verdict"] == "unstamped"


def test_sidecar_carries_the_source_audit_verdict_when_the_audit_covers_the_range(tmp_path, monkeypatch) -> None:
    payload = _save_with_report(tmp_path, monkeypatch, _audit_report("2026-06-01", "2026-07-31"))

    assert payload["source_quality_verdict"] == "pass"
    assert payload["source_quality_report_sha256"] == "a" * 64
    assert payload["source_quality_range"] == ["2026-06-01", "2026-07-31"]


def test_sidecar_flags_a_source_audit_that_does_not_cover_the_requested_range(tmp_path, monkeypatch) -> None:
    payload = _save_with_report(tmp_path, monkeypatch, _audit_report("2026-01-01", "2026-02-01"))

    # A stale audit is worse than none if it reads as coverage, so the mismatch is
    # named and the requested window recorded alongside it.
    assert payload["source_quality_verdict"] == "unstamped_range_mismatch"
    assert payload["source_quality_requested_range"] == ["2026-07-02", "2026-07-06"]


def test_source_quality_stamp_is_covered_by_the_sidecar_metadata_hash(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        smma_dataset_module, "load_latest_report", lambda *_a, **_k: _audit_report("2026-06-01", "2026-07-31")
    )
    dataset = rows_to_bar_dataset(_causal_rows())
    path = tmp_path / "dataset.npz"
    _output, sidecar = save_governed_dataset(
        path,
        dataset,
        query_evidence=[{"query_sha256": "abc", "guard_overall": "pass"}],
        code_fingerprint="code",
    )

    payload = json.loads(sidecar.read_text())
    assert payload["source_quality_verdict"] == "pass"
    payload["source_quality_verdict"] = "fail"  # rewrite the verdict by hand
    sidecar.write_text(json.dumps(payload))

    # The stamp is merged before the hash is computed, so rewriting the verdict
    # invalidates the sidecar instead of silently changing the artifact's story.
    with pytest.raises(DatasetGovernanceError, match="sidecar fingerprint mismatch"):
        load_governed_dataset(path)
