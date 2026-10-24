"""Tick integrity columns (total_volume, is_simtrade, is_odd_lot) reach hft.market_data."""

from __future__ import annotations

from pathlib import Path

from hft_platform.events import BidAskEvent, MetaData, TickEvent
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.recorder._loader_batch import _MARKET_DATA_COLS, format_market_data
from hft_platform.recorder.mapper import map_event_to_record
from hft_platform.recorder.worker import MARKET_DATA_COLUMNS, _extract_market_data_values

NEW_COLUMNS = ("total_volume", "is_simtrade", "is_odd_lot")
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "hft_platform"
    / "migrations"
    / "clickhouse"
    / "20261006_001_add_tick_integrity_columns.sql"
)


def _metadata(tmp_path: Path) -> SymbolMetadata:
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: 'AAA'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    return SymbolMetadata(str(cfg))


def _tick(**kwargs: object) -> TickEvent:
    meta = MetaData(seq=1, topic="tick", source_ts=10, local_ts=20)
    return TickEvent(meta=meta, symbol="AAA", price=123450, volume=5, **kwargs)  # type: ignore[arg-type]


def _value(values: list, column: str) -> object:
    return values[MARKET_DATA_COLUMNS.index(column)]


def test_worker_and_loader_column_lists_carry_the_new_columns_in_the_same_order() -> None:
    assert MARKET_DATA_COLUMNS == _MARKET_DATA_COLS
    assert tuple(MARKET_DATA_COLUMNS[-3:]) == NEW_COLUMNS


def test_mapper_records_the_exchange_cumulative_volume_and_flags_of_a_tick(tmp_path: Path) -> None:
    event = _tick(total_volume=48_211, is_simtrade=True, is_odd_lot=True)

    _, row = map_event_to_record(event, _metadata(tmp_path))

    assert row["total_volume"] == 48_211
    assert row["is_simtrade"] == 1
    assert row["is_odd_lot"] == 1


def test_mapper_flags_are_zero_for_a_normal_round_lot_tick(tmp_path: Path) -> None:
    _, row = map_event_to_record(_tick(total_volume=7), _metadata(tmp_path))

    assert (row["is_simtrade"], row["is_odd_lot"]) == (0, 0)


def test_worker_extracts_the_new_values_from_a_mapped_tick_dict(tmp_path: Path) -> None:
    _, row = map_event_to_record(_tick(total_volume=900, is_simtrade=True), _metadata(tmp_path))

    values = _extract_market_data_values(row)

    assert values is not None and len(values) == len(MARKET_DATA_COLUMNS)
    assert _value(values, "total_volume") == 900
    assert _value(values, "is_simtrade") == 1
    assert _value(values, "is_odd_lot") == 0


def test_worker_extracts_the_new_values_from_an_attribute_row() -> None:
    class Row:
        symbol = "AAA"
        type = "Tick"
        exch_ts = 10
        ingest_ts = 20
        price_scaled = 1
        volume = 2
        total_volume = 33
        is_simtrade = 0
        is_odd_lot = 1

    values = _extract_market_data_values(Row())

    assert values is not None
    assert (_value(values, "total_volume"), _value(values, "is_odd_lot")) == (33, 1)


def test_a_row_written_before_the_migration_reads_as_not_recorded() -> None:
    old_row = {"symbol": "AAA", "type": "Tick", "exch_ts": 10, "ingest_ts": 20, "price_scaled": 1, "volume": 2}

    values = _extract_market_data_values(old_row)

    assert values is not None
    assert [_value(values, c) for c in NEW_COLUMNS] == [0, 0, 0]


def test_wal_replay_of_an_old_row_fills_the_new_columns_with_defaults() -> None:
    old_row = {"symbol": "AAA", "type": "Tick", "exch_ts": 10, "ingest_ts": 20, "price_scaled": 1, "volume": 2}

    cols, data = format_market_data([old_row])

    assert [data[0][cols.index(c)] for c in NEW_COLUMNS] == [0, 0, 0]


def test_wal_replay_keeps_the_new_values_when_the_row_has_them() -> None:
    row = {
        "symbol": "AAA",
        "type": "Tick",
        "exch_ts": 10,
        "ingest_ts": 20,
        "price_scaled": 1,
        "volume": 2,
        "total_volume": 77,
        "is_simtrade": 1,
        "is_odd_lot": 0,
    }

    cols, data = format_market_data([row])

    assert [data[0][cols.index(c)] for c in NEW_COLUMNS] == [77, 1, 0]


def test_a_book_row_carries_no_tick_integrity_values(tmp_path: Path) -> None:
    meta = MetaData(seq=2, topic="bidask", source_ts=11, local_ts=21)
    event = BidAskEvent(meta=meta, symbol="AAA", bids=[[10000, 1]], asks=[[11000, 2]])
    _, row = map_event_to_record(event, _metadata(tmp_path))

    values = _extract_market_data_values(row)

    assert values is not None
    assert [_value(values, c) for c in NEW_COLUMNS] == [0, 0, 0]


def test_the_migration_only_adds_the_three_columns_with_defaults() -> None:
    statements = [
        line.strip()
        for line in MIGRATION.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]

    assert statements == [
        "ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS total_volume Int64 DEFAULT 0;",
        "ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS is_simtrade UInt8 DEFAULT 0;",
        "ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS is_odd_lot UInt8 DEFAULT 0;",
    ]
