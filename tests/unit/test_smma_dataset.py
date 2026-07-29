from __future__ import annotations

import json

import numpy as np
import pytest

from research.combinatorial.smma_dataset import (
    DatasetGovernanceError,
    _bar_query,
    _guard_query,
    load_governed_dataset,
    rows_to_bar_dataset,
    save_governed_dataset,
)


def _row(root: str, contract: str, day: str, ts: int, volume: int) -> tuple[object, ...]:
    price = 100.0 + (ts / 1_000_000_000_000)
    return (
        root,
        contract,
        day,
        "day",
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


def test_dataset_query_guard_blocks_mutation() -> None:
    with pytest.raises(DatasetGovernanceError):
        _guard_query("DROP TABLE hft.market_data")


def test_bar_query_casts_datetime_bucket_before_nanosecond_conversion() -> None:
    query = _bar_query(60, date_from="2026-03-19", date_to="2026-07-24")
    assert "toUnixTimestamp64Nano(toDateTime64(bucket, 9, 'Asia/Taipei'))" in query
    assert "toDateTime64('1970-01-01 08:45:00', 9, 'Asia/Taipei')" in query
    assert "toDateTime64('1970-01-01 15:00:00', 9, 'Asia/Taipei')" in query


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
