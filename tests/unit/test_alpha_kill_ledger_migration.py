"""Slice-D Task 3: structural checks for the audit.alpha_kill_ledger migration.

The kill ledger is the durable record of every Gate-A..F / pre_screen /
cluster / manual rejection. Idempotency is enforced by the writer
(kill_ledger.append_kill) via a deterministic kill_id; the migration's job
is to make sure the schema stays in lock-step with that contract.
"""
from __future__ import annotations

from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "hft_platform"
    / "migrations"
    / "clickhouse"
    / "20260505_001_create_alpha_kill_ledger.sql"
)


def test_migration_file_exists() -> None:
    assert MIGRATION.is_file(), f"migration SQL missing at {MIGRATION}"


def test_migration_creates_alpha_kill_ledger() -> None:
    src = MIGRATION.read_text()
    assert "CREATE TABLE IF NOT EXISTS audit.alpha_kill_ledger" in src


def test_migration_kill_id_is_first_column_and_required() -> None:
    """kill_id must lead the column list and be NOT NULL — it's the dedupe key."""
    src = MIGRATION.read_text()
    assert "kill_id              String                      NOT NULL" in src, (
        "kill_id must be String NOT NULL — it is the deterministic dedupe key"
    )
    column_block = src.split("CREATE TABLE IF NOT EXISTS audit.alpha_kill_ledger (", 1)[1]
    first_column = column_block.split(",", 1)[0]
    assert "kill_id" in first_column, "kill_id must be the first column"


def test_migration_order_by_kill_id_for_dedupe() -> None:
    """ORDER BY must include kill_id so the (alpha_id, kill_id) dedupe pre-check is index-aligned."""
    src = MIGRATION.read_text()
    assert "ORDER BY (alpha_id, kill_id, killed_at)" in src, (
        "ORDER BY must be (alpha_id, kill_id, killed_at) for dedupe pre-check + audit ordering"
    )


def test_migration_partition_and_ttl() -> None:
    src = MIGRATION.read_text()
    assert "PARTITION BY toYYYYMM(killed_at)" in src, "monthly partitions on killed_at expected"
    assert "TTL killed_at + INTERVAL 365 DAY" in src, (
        "365-day TTL aligns with hft.orders / hft.order_intents retention"
    )


def test_migration_gate_enum_covers_all_paths() -> None:
    """gate enum must cover A..F + pre_screen + cluster + manual (9 values)."""
    src = MIGRATION.read_text()
    for label, code in [
        ("A", 1), ("B", 2), ("C", 3), ("D", 4), ("E", 5), ("F", 6),
        ("pre_screen", 7), ("cluster", 8), ("manual", 9),
    ]:
        assert f"'{label}'={code}" in src, f"gate enum missing {label}={code}"


def test_migration_engine_is_mergetree_not_replacing() -> None:
    """Writer-side dedupe is the source of truth; no ReplacingMergeTree merge dependency."""
    src = MIGRATION.read_text()
    assert "ENGINE = MergeTree" in src
    assert "ReplacingMergeTree" not in src, (
        "ReplacingMergeTree would mask writer-side dedupe bugs by relying on async merges"
    )


def test_migration_stable_artifact_hash_default_empty() -> None:
    """Empty default keeps backfill pathway for archived alphas without a manifest hash."""
    src = MIGRATION.read_text()
    assert "stable_artifact_hash String                      DEFAULT ''" in src
