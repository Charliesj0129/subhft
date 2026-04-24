"""C2 / Phase D: verify the hft.fills ReplacingMergeTree migration SQL is
present and structurally correct before it is applied to a live cluster.

Root cause being addressed: WAL replay dedup depended entirely on the
hft._wal_dedup guard table. Any gap there (schema drift in the hasher,
missed dedup write, or operator-invoked `psql --replay`) silently
double-inserts fills. Moving hft.fills to ReplacingMergeTree keyed by
fill_id forces ClickHouse to collapse duplicates at merge time — a
defence-in-depth layer that does not rely on the dedup guard.
"""

from __future__ import annotations

from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "hft_platform"
    / "migrations"
    / "clickhouse"
    / "20260425_001_fills_replacing_merge_tree.sql"
)


def test_migration_file_exists():
    assert MIGRATION.is_file(), f"migration SQL missing at {MIGRATION}"


def test_migration_uses_replacing_merge_tree():
    src = MIGRATION.read_text()
    assert "ReplacingMergeTree()" in src, (
        "ReplacingMergeTree engine must be specified for hft.fills_new"
    )


def test_migration_order_by_includes_fill_id():
    src = MIGRATION.read_text()
    # ORDER BY must end with fill_id so the dedup key uniquely identifies
    # the row. Without fill_id in ORDER BY, ReplacingMergeTree collapses
    # by (strategy_id, symbol, ts_exchange) which is not unique per fill.
    assert "ORDER BY (strategy_id, symbol, ts_exchange, fill_id)" in src, (
        "fill_id must be the last ORDER BY column for unique-per-fill dedup"
    )


def test_migration_preserves_legacy_for_rollback():
    src = MIGRATION.read_text()
    # Explicit rename keeps the old table around for at least one day so
    # operators can cross-check row counts before dropping.
    assert "hft.fills_legacy_pre_rmt" in src, (
        "legacy table must be preserved under hft.fills_legacy_pre_rmt for rollback"
    )
    assert "RENAME TABLE hft.fills TO hft.fills_legacy_pre_rmt" in src


def test_migration_copies_existing_rows():
    src = MIGRATION.read_text()
    # INSERT INTO hft.fills_new SELECT ... FROM hft.fills must be present
    # so the RENAME does not leave a truncated fills table.
    assert "INSERT INTO hft.fills_new" in src
    assert "FROM hft.fills" in src
