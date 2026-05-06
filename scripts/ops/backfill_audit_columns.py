#!/usr/bin/env python3
"""Backfill the L7 audit columns on hft.orders and hft.fills.

Idempotent operator tool. ClickHouse's ``ADD COLUMN ... DEFAULT ''`` already
makes existing rows return the empty string at SELECT time, so the only
purpose of this script is operational visibility:

  * Counts rows with empty audit fields and reports them.
  * Optionally issues no-op ``ALTER TABLE ... UPDATE`` mutations to materialize
    the default into the underlying parts (useful when an operator wants the
    columns physically present rather than virtual). Off by default
    (``--materialize`` opt-in).

Usage::

    python scripts/ops/backfill_audit_columns.py --dry-run
    python scripts/ops/backfill_audit_columns.py --batch-size 50000
    python scripts/ops/backfill_audit_columns.py --materialize

Exits non-zero on connection failure or partial-migration state. The dual-write
mapper in recorder/writer.py also refuses to start in partial-migration state;
this script's check is operator-facing.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("backfill_audit_columns")

L7_AUDIT_COLUMNS: tuple[str, ...] = (
    "trace_id",
    "feature_snapshot_id",
    "risk_decision_id",
    "strategy_version",
    "config_hash",
    "git_sha",
    "data_session_id",
)

L7_TABLES: tuple[str, ...] = ("hft.orders", "hft.fills")
L7_MIGRATION_VERSIONS: tuple[str, str] = ("20260505_001", "20260505_002")


def _build_client(host: str, port: int) -> Any:
    try:
        import clickhouse_connect
    except ImportError as exc:  # pragma: no cover - operator env
        raise SystemExit(
            "clickhouse_connect is not installed; run inside the project venv."
        ) from exc

    user = os.environ.get("HFT_CLICKHOUSE_USER", "default")
    password = os.environ.get("HFT_CLICKHOUSE_PASSWORD", "")
    interface = "native" if port == 9000 else "http"
    kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "username": user,
        "password": password,
    }
    if interface == "native":
        kwargs["interface"] = "native"
    return clickhouse_connect.get_client(**kwargs)


def _check_migration_state(client: Any) -> None:
    """Refuse to run in partial-migration state."""
    v_orders, v_fills = L7_MIGRATION_VERSIONS
    try:
        result = client.query(
            "SELECT version FROM hft.schema_migrations "
            f"WHERE version IN ('{v_orders}', '{v_fills}')"
        )
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"Failed to query hft.schema_migrations: {exc}. "
            "Verify the recorder schema has been initialized at least once."
        ) from exc

    applied = {row[0] for row in result.result_rows}
    orders_applied = v_orders in applied
    fills_applied = v_fills in applied

    if orders_applied != fills_applied:
        missing = v_orders if not orders_applied else v_fills
        applied_one = v_orders if orders_applied else v_fills
        raise SystemExit(
            f"L7 partial-migration state detected: applied={applied_one}, missing={missing}. "
            "Run apply_schema or roll back the applied half before backfilling."
        )

    if not orders_applied and not fills_applied:
        raise SystemExit(
            "Neither L7 audit migration is applied. Run `apply_schema` first "
            "(or `make migrate`); there is nothing for this script to backfill."
        )

    logger.info("l7_migration_state_verified versions=%s", sorted(applied))


def _count_rows(client: Any, table: str) -> int:
    try:
        result = client.query(f"SELECT count() FROM {table}")
        return int(result.result_rows[0][0])
    except Exception as exc:  # noqa: BLE001
        logger.warning("count_failed table=%s error=%s", table, exc)
        return -1


def _count_empty_audit_rows(client: Any, table: str) -> int:
    """Count rows where every L7 audit column is empty."""
    where = " AND ".join(f"{col} = ''" for col in L7_AUDIT_COLUMNS)
    try:
        result = client.query(f"SELECT count() FROM {table} WHERE {where}")
        return int(result.result_rows[0][0])
    except Exception as exc:  # noqa: BLE001
        logger.warning("empty_audit_count_failed table=%s error=%s", table, exc)
        return -1


def _materialize(client: Any, table: str, batch_size: int) -> None:
    """Issue no-op ``ALTER TABLE ... UPDATE`` to materialize the column defaults.

    The mutation predicate is intentionally a no-op (``WHERE trace_id = ''``
    setting ``trace_id = ''``); ClickHouse still rewrites affected parts so
    the column physically exists in storage. Mutations are async; the script
    does NOT wait for completion. Operator should monitor
    ``system.mutations`` for progress.
    """
    for col in L7_AUDIT_COLUMNS:
        stmt = (
            f"ALTER TABLE {table} UPDATE {col} = '' "
            f"WHERE {col} = '' SETTINGS mutations_sync = 0"
        )
        logger.info("materialize_submit table=%s column=%s batch_size=%s", table, col, batch_size)
        try:
            client.command(stmt)
        except Exception as exc:  # noqa: BLE001
            logger.error("materialize_failed table=%s column=%s error=%s", table, col, exc)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("HFT_CLICKHOUSE_HOST", "localhost"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("HFT_CLICKHOUSE_PORT", "9000")),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50_000,
        help="Advisory batch hint passed through to ALTER UPDATE; ClickHouse "
        "decides actual mutation chunking.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report row counts without issuing any mutations.",
    )
    parser.add_argument(
        "--materialize",
        action="store_true",
        help="Issue no-op ALTER UPDATE statements to physically materialize the "
        "audit column defaults. Off by default — defaults already work at SELECT "
        "time, so most operators do not need this.",
    )
    args = parser.parse_args()

    if args.dry_run and args.materialize:
        logger.error("dry_run_and_materialize_are_mutually_exclusive")
        return 2

    client = _build_client(args.host, args.port)
    _check_migration_state(client)

    for table in L7_TABLES:
        total = _count_rows(client, table)
        empty = _count_empty_audit_rows(client, table)
        logger.info("audit_status table=%s total_rows=%s empty_audit_rows=%s", table, total, empty)

    if args.dry_run:
        logger.info("dry_run_complete")
        return 0

    if args.materialize:
        for table in L7_TABLES:
            _materialize(client, table, args.batch_size)
        logger.info("materialize_submitted_check_system_mutations")
        return 0

    logger.info("no_action_taken_pass_--materialize_to_rewrite_parts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
