from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from research.combinatorial.taifex_trading_dates import build_trading_date_window
from research.combinatorial.tick_dataset import (
    TICK_DATASET_SCHEMA,
    TickBarDataset,
    TickDatasetGovernanceError,
    _guard_query,
    _restrict_to_full_sessions,
    _tick_bar_query,
    export_clickhouse_tick_dataset,
    load_governed_tick_dataset,
    rows_to_tick_bar_dataset,
    save_governed_tick_dataset,
)


def _row(
    root: str,
    contract: str,
    day: str,
    ts: int,
    trade_ticks: int,
    *,
    session: str = "day",
) -> tuple[object, ...]:
    price = 100.0 + (ts / 1_000_000_000_000)
    buy = trade_ticks // 2
    sell = trade_ticks - buy
    unknown = trade_ticks - buy - sell
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
        price - 0.1,
        price + 0.1,
        price - 0.05,
        price + 0.05,
        trade_ticks,
        trade_ticks * 4,
        buy,
        sell,
        unknown,
        buy * 2,
        sell * 3,
    )


def _causal_rows() -> dict[int, list[tuple[object, ...]]]:
    rows: list[tuple[object, ...]] = []
    day_specs = (
        ("2026-07-01", 100, 50),
        ("2026-07-02", 10, 200),
        ("2026-07-03", 20, 300),
        ("2026-07-06", 30, 400),
    )
    for day_index, (day, ticks_a, ticks_b) in enumerate(day_specs):
        base = (day_index + 1) * 10_000_000_000_000
        for offset in (0, 3_600_000_000_000):
            rows.append(_row("TXF", "TXFA6", day, base + offset, ticks_a))
            rows.append(_row("TXF", "TXFB6", day, base + offset, ticks_b))
    return {60: rows}


def _complete_export_rows(*, degenerate_warmup: bool = False) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    trading_days = ("2026-06-30", "2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06")
    for day_index, day in enumerate(trading_days):
        for root in ("TXF", "TMF"):
            contract = f"{root}D6"
            base = (day_index + 1) * 100_000_000_000_000
            for offset in range(14):
                row = _row(
                    root,
                    contract,
                    day,
                    base + offset * 3_600_000_000_000,
                    100,
                    session="night",
                )
                if degenerate_warmup and day == "2026-06-30":
                    mutable = list(row)
                    mutable[15] = 0
                    mutable[16] = 0
                    mutable[17] = 100
                    row = tuple(mutable)
                rows.append(row)
            for offset in range(5):
                row = _row(
                    root,
                    contract,
                    day,
                    base + (14 + offset) * 3_600_000_000_000,
                    100,
                    session="day",
                )
                if degenerate_warmup and day == "2026-06-30":
                    mutable = list(row)
                    mutable[15] = 0
                    mutable[16] = 0
                    mutable[17] = 100
                    row = tuple(mutable)
                rows.append(row)
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


def test_tick_dataset_front_contract_uses_previous_day_tick_count_and_resets_on_roll() -> None:
    dataset = rows_to_tick_bar_dataset(_causal_rows())
    assert set(dataset.trading_day) == {"2026-07-02", "2026-07-03", "2026-07-06"}
    by_day = {day: set(dataset.contract[dataset.trading_day == day]) for day in set(dataset.trading_day)}
    assert by_day["2026-07-02"] == {"TXFA6"}
    assert by_day["2026-07-03"] == {"TXFB6"}
    assert by_day["2026-07-06"] == {"TXFB6"}
    roll_index = int(np.flatnonzero(dataset.trading_day == "2026-07-03")[0])
    assert dataset.reset[roll_index]


def test_tick_dataset_resets_after_one_missing_intraday_bar() -> None:
    rows = _causal_rows()[60]
    rows = [
        row
        for row in rows
        if not (row[2] == "2026-07-03" and row[1] == "TXFB6" and int(row[4]) == 30_000_000_000_000 + 3_600_000_000_000)
    ]
    rows.append(_row("TXF", "TXFB6", "2026-07-03", 30_000_000_000_000 + 7_200_000_000_000, 300))
    dataset = rows_to_tick_bar_dataset({60: rows})
    day_indices = np.flatnonzero(dataset.trading_day == "2026-07-03")
    assert day_indices.size == 2
    assert dataset.reset[day_indices[-1]]


def test_tick_dataset_aligns_root_groups_to_common_days_and_resets_after_exclusion() -> None:
    day_values = ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06")
    rows: list[tuple[object, ...]] = []
    for day_index, day in enumerate(day_values):
        timestamp = day_index * 86_400_000_000_000
        rows.append(_row("TXF", "TXFA6", day, timestamp, 100))
        if day != "2026-07-03":
            rows.append(_row("TMF", "TMFA6", day, timestamp, 100))

    dataset = rows_to_tick_bar_dataset({60: rows})

    assert set(dataset.trading_day) == {"2026-07-02", "2026-07-06"}
    assert set(dataset.trading_day[dataset.root == "TXF"]) == set(dataset.trading_day[dataset.root == "TMF"])
    txf_last = int(np.flatnonzero(dataset.root == "TXF")[-1])
    assert dataset.reset[txf_last]


def test_tick_dataset_validate_rejects_aggressor_counts_that_do_not_sum_to_trade_count() -> None:
    dataset = rows_to_tick_bar_dataset(_causal_rows())
    tampered = TickBarDataset(
        **{
            field: (
                np.asarray(getattr(dataset, field)) + 1.0
                if field == "buy_tick_count"
                else np.asarray(getattr(dataset, field))
            )
            for field in dataset.__dataclass_fields__
        }
    )
    with pytest.raises(TickDatasetGovernanceError, match="must equal trade_tick_count"):
        tampered.validate()


def test_tick_export_is_rejected_when_trade_direction_is_degenerate() -> None:
    dataset = rows_to_tick_bar_dataset(_causal_rows())
    degenerate = TickBarDataset(
        **{
            field: (
                np.zeros(len(dataset), dtype=np.float64)
                if field in {"buy_tick_count", "sell_tick_count"}
                else (
                    np.asarray(dataset.trade_tick_count, dtype=np.float64)
                    if field == "unknown_tick_count"
                    else np.asarray(getattr(dataset, field))
                )
            )
            for field in dataset.__dataclass_fields__
        }
    )

    with pytest.raises(TickDatasetGovernanceError, match="unknown aggressor ratio"):
        degenerate.validate()


def test_tick_export_accepts_a_day_just_under_unknown_tick_threshold() -> None:
    dataset = rows_to_tick_bar_dataset(_causal_rows())
    trade = np.asarray(dataset.trade_tick_count, dtype=np.float64)
    unknown = trade * 0.049
    accepted = TickBarDataset(
        **{
            field: (
                trade - unknown
                if field == "buy_tick_count"
                else (
                    np.zeros(len(dataset), dtype=np.float64)
                    if field == "sell_tick_count"
                    else (unknown if field == "unknown_tick_count" else np.asarray(getattr(dataset, field)))
                )
            )
            for field in dataset.__dataclass_fields__
        }
    )

    accepted.validate()
    assert len(accepted) == len(dataset)


def test_tick_dataset_validate_rejects_bars_with_no_trade_ticks() -> None:
    dataset = rows_to_tick_bar_dataset(_causal_rows())
    zeroed = TickBarDataset(
        **{
            field: (
                np.zeros(len(dataset), dtype=np.float64)
                if field in {"trade_tick_count", "buy_tick_count", "sell_tick_count", "unknown_tick_count"}
                else np.asarray(getattr(dataset, field))
            )
            for field in dataset.__dataclass_fields__
        }
    )
    with pytest.raises(TickDatasetGovernanceError, match="at least one trade tick"):
        zeroed.validate()


def test_tick_dataset_validate_rejects_negative_aggregate_counts() -> None:
    dataset = rows_to_tick_bar_dataset(_causal_rows())
    negative = TickBarDataset(
        **{
            field: (
                np.full(len(dataset), -1.0) if field == "quote_update_count" else np.asarray(getattr(dataset, field))
            )
            for field in dataset.__dataclass_fields__
        }
    )
    with pytest.raises(TickDatasetGovernanceError, match="finite and non-negative"):
        negative.validate()


def test_tick_dataset_group_selects_one_root_timeframe_and_rejects_unknown_groups() -> None:
    dataset = rows_to_tick_bar_dataset(_causal_rows())
    group = dataset.group("TXF", 60)
    assert set(group.root) == {"TXF"}
    assert set(group.timeframe_min) == {60}
    with pytest.raises(TickDatasetGovernanceError, match="no tick bars"):
        dataset.group("TMF", 60)


def test_tick_dataset_sidecar_round_trips_and_records_phase_one_aggregate_rule(tmp_path) -> None:
    dataset = rows_to_tick_bar_dataset(_causal_rows())
    path = tmp_path / "dataset.npz"
    _output, sidecar = save_governed_tick_dataset(
        path,
        dataset,
        query_evidence=[{"query_sha256": "abc", "guard_overall": "pass"}],
        code_fingerprint="code",
    )
    loaded = load_governed_tick_dataset(path)
    assert len(loaded) == len(dataset)
    np.testing.assert_allclose(loaded.trade_tick_count, dataset.trade_tick_count)
    np.testing.assert_allclose(loaded.buy_tick_volume, dataset.buy_tick_volume)
    payload = json.loads(sidecar.read_text())
    assert payload["schema"] == TICK_DATASET_SCHEMA
    assert payload["price_scale_source"] == 1_000_000
    assert payload["aggregate_rule"].startswith("Phase 1")
    assert payload["roll_rule"].startswith("front contract selected by previous")


def test_tick_dataset_sidecar_detects_content_tamper(tmp_path) -> None:
    dataset = rows_to_tick_bar_dataset(_causal_rows())
    path = tmp_path / "dataset.npz"
    save_governed_tick_dataset(
        path,
        dataset,
        query_evidence=[{"query_sha256": "abc", "guard_overall": "pass"}],
        code_fingerprint="code",
    )
    with path.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(TickDatasetGovernanceError, match="content fingerprint mismatch"):
        load_governed_tick_dataset(path)


def test_tick_dataset_sidecar_detects_metadata_tamper(tmp_path) -> None:
    dataset = rows_to_tick_bar_dataset(_causal_rows())
    path = tmp_path / "dataset.npz"
    _output, sidecar = save_governed_tick_dataset(
        path,
        dataset,
        query_evidence=[{"query_sha256": "abc", "guard_overall": "pass"}],
        code_fingerprint="code",
    )
    payload = json.loads(sidecar.read_text())
    payload["roll_rule"] = "same-day volume"
    sidecar.write_text(json.dumps(payload))
    with pytest.raises(TickDatasetGovernanceError, match="sidecar fingerprint mismatch"):
        load_governed_tick_dataset(path)


def test_tick_dataset_query_guard_blocks_mutation() -> None:
    with pytest.raises(TickDatasetGovernanceError):
        _guard_query("DROP TABLE hft.market_data")


def test_tick_bar_query_aggregates_aggressor_split_and_passes_the_guard() -> None:
    query = _tick_bar_query(60, date_from="2026-03-19", date_to="2026-07-24")

    assert "countIf(type = 'Tick' AND price_scaled > 0) AS trade_tick_count" in query
    assert "countIf(type = 'BidAsk') AS quote_update_count" in query
    assert "trade_direction = 1) AS buy_tick_count" in query
    assert "trade_direction = -1) AS sell_tick_count" in query
    assert "trade_direction = 0) AS unknown_tick_count" in query
    assert "sumIf(volume, type = 'Tick' AND price_scaled > 0 AND trade_direction = 1) AS buy_tick_volume" in query
    assert "toUnixTimestamp64Nano(toDateTime64(bucket, 9, 'Asia/Taipei'))" in query
    assert _guard_query(query)["guard_overall"] == "pass"


def test_tick_bar_query_two_minute_lane_is_guarded_with_a_nontruncating_cap() -> None:
    query = _tick_bar_query(2, date_from="2026-03-19", date_to="2026-07-24")

    assert "INTERVAL 2 MINUTE" in query
    assert "LIMIT 1000000" in query
    assert _guard_query(query)["guard_overall"] == "pass"


def test_tick_bar_daily_query_aggregates_full_trading_day() -> None:
    query = _tick_bar_query(1440, date_from="2026-03-19", date_to="2026-07-24")

    assert "'full' AS session" in query
    assert "toStartOfDay(toDateTime(trading_day, 'Asia/Taipei')) AS bucket" in query
    assert "INTERVAL 1440 MINUTE" not in query
    assert _guard_query(query)["guard_overall"] == "pass"


def test_tick_bar_query_rejects_unsupported_timeframe() -> None:
    with pytest.raises(ValueError, match="unsupported timeframe"):
        _tick_bar_query(7, date_from="2026-03-19", date_to="2026-07-24")


def test_tick_export_runs_readonly_bounded_queries_and_writes_query_evidence(tmp_path) -> None:
    client = _FakeClient(_complete_export_rows(degenerate_warmup=True))
    path = tmp_path / "dataset.npz"

    _output, sidecar = export_clickhouse_tick_dataset(
        path,
        code_fingerprint="code",
        client=client,
        date_from="2026-07-01",
        date_to="2026-07-06",
        timeframes_minutes=(60,),
    )

    assert len(client.queries) == 1
    settings = client.settings[0]
    assert settings["readonly"] == 1
    assert settings["result_overflow_mode"] == "throw"
    assert settings["max_result_rows"] == 100_000
    payload = json.loads(sidecar.read_text())
    assert payload["requested_date_from"] == "2026-07-01"
    assert payload["requested_date_to"] == "2026-07-06"
    assert payload["query_evidence"][0]["timeframe_min"] == 60
    assert payload["query_evidence"][0]["result_rows"] == len(_complete_export_rows())
    assert payload["schema_version"] == 2
    assert payload["governance_complete"] is True
    assert payload["eligible_trading_dates"] == [
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
        "2026-07-06",
    ]
    assert load_governed_tick_dataset(path) is not None


def test_partial_sixty_minute_day_is_excluded_from_every_timeframe() -> None:
    rows = _complete_export_rows()
    partial = [
        row
        for row in rows
        if not (row[0] == "TMF" and row[2] == "2026-07-03" and row[3] == "night" and int(row[4]) == 400_000_000_000_000)
    ]
    daily = [
        _row(root, f"{root}D6", day, (index + 1) * 100_000_000_000_000, 100, session="full")
        for index, day in enumerate(("2026-06-30", "2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06"))
        for root in ("TXF", "TMF")
    ]
    selected = rows_to_tick_bar_dataset({60: partial, 1440: daily}, align_common=False)

    restricted, evidence = _restrict_to_full_sessions(
        selected,
        build_trading_date_window("2026-07-01", "2026-07-06"),
    )

    assert "2026-07-03" not in set(restricted.trading_day)
    assert set(restricted.timeframe_min) == {60, 1440}
    assert evidence.excluded_partial_trading_dates[0]["trading_date"] == "2026-07-03"


def test_tick_export_rejects_duplicate_or_unsupported_timeframes(tmp_path) -> None:
    client = _FakeClient(_causal_rows()[60])
    with pytest.raises(ValueError, match="distinct supported values"):
        export_clickhouse_tick_dataset(
            tmp_path / "dataset.npz",
            code_fingerprint="code",
            client=client,
            timeframes_minutes=(60, 60),
        )
    with pytest.raises(ValueError, match="distinct supported values"):
        export_clickhouse_tick_dataset(
            tmp_path / "dataset.npz",
            code_fingerprint="code",
            client=client,
            timeframes_minutes=(7,),
        )


def test_tick_export_refuses_a_result_that_reached_the_row_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("research.combinatorial.tick_dataset._query_row_limit", lambda _timeframe: 1)
    client = _FakeClient(_causal_rows()[60])
    with pytest.raises(TickDatasetGovernanceError, match="refusing possible truncation"):
        export_clickhouse_tick_dataset(
            tmp_path / "dataset.npz",
            code_fingerprint="code",
            client=client,
            timeframes_minutes=(60,),
        )
