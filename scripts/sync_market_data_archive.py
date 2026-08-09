#!/usr/bin/env python3
"""Sync missing ``hft.market_data`` partitions from the production host into the local archive.

Why this exists
---------------
The local ClickHouse corpus is the **only durable copy** of research market data: its
TTL was removed on 2026-07-25 and the L2 NPZ corpus was deleted on 2026-07-20. The
production host still carries the DDL's 6-month TTL, so anything not pulled before a
partition's expiry is gone permanently.

The pull procedure was previously implemented in a scratchpad script that did not
survive its session -- ``docs/operations/local-clickhouse-market-data-corpus.md`` still
cites ``scratchpad/pull_theshow_market_data.sh``, which no longer exists anywhere. This
module restores that procedure as versioned, repeatable, hash-verified tooling.

Safety properties
-----------------
* **Read-only on production.** Every remote statement is a ``SELECT``. Nothing is
  written, altered, or dropped on the remote host.
* **Refuses partitions that already hold local rows.** ``market_data`` is a plain
  ``MergeTree``: re-inserting an existing partition *duplicates* rather than replaces.
  This is why the 2-row shortfall on partition ``20260804`` cannot be repaired by
  re-pulling it -- that would duplicate 7.3M rows.
* **Skips the in-flight partition** (today, UTC) unless ``--include-today``, because a
  partition still being written cannot be hash-verified.
* **Verifies before and after** with ``count()`` plus a ``cityHash64`` row digest on
  both sides. A mismatch aborts and prints the exact remediation command rather than
  silently dropping data.
* **Dry-run is the default.** Writing requires an explicit ``--sync`` flag.

Credentials are read from the environment or ``.env`` and never appear in argv or output.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

TABLE = "hft.market_data"
DATABASE, TABLE_NAME = TABLE.split(".", 1)

# Matches the table's own PARTITION BY expression, so a partition is selected by the
# same rule that defined it. Deliberately not an ingest_ts range: that would reintroduce
# the boundary ambiguity the partition key already resolves.
PARTITION_EXPR = "toYYYYMMDD(toDateTime(ingest_ts / 1000000000))"

# The documented digest for this table. Covers identity, both timestamps, and the
# payload fields that a partial transfer would truncate.
ROW_DIGEST_EXPR = "sum(cityHash64(symbol, exch_ts, ingest_ts, price_scaled, volume, seq_no))"

REMOTE_ENV_VAR = "HFT_ARCHIVE_REMOTE"
SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=15")
COPY_CHUNK_BYTES = 1 << 20

# A missing partition whose remote TTL fires within this horizon is not merely "behind" --
# it is actively about to become unrecoverable.
URGENT_HORIZON_DAYS = 30


class SyncError(RuntimeError):
    """Raised when an invariant that protects the archive is violated."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env_or_dotenv(key: str, *, env_path: Path = Path(".env")) -> str:
    """Read a setting from the environment, falling back to ``.env``.

    Mirrors ``research.data_pipeline._dotenv_value``; duplicated deliberately so this
    ops script stays importable without pulling in numpy and the platform package.
    """
    value = os.environ.get(key)
    if value:
        return str(value)
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, raw = stripped.split("=", 1)
        if name.strip() == key:
            return raw.strip().strip('"').strip("'")
    return ""


@dataclass(frozen=True, slots=True)
class PartitionInfo:
    partition: str
    rows: int
    ttl_expiry: str | None = None

    @property
    def days_until_expiry(self) -> int | None:
        if not self.ttl_expiry or self.ttl_expiry.startswith("1970-01-01"):
            return None
        try:
            expiry = datetime.fromisoformat(self.ttl_expiry).replace(tzinfo=UTC)
        except ValueError:
            return None
        return (expiry - datetime.now(UTC)).days


@dataclass(slots=True)
class Inventory:
    local: dict[str, PartitionInfo] = field(default_factory=dict)
    remote: dict[str, PartitionInfo] = field(default_factory=dict)

    @property
    def missing_local(self) -> list[PartitionInfo]:
        return [info for name, info in sorted(self.remote.items()) if name not in self.local]

    @property
    def row_deltas(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name, remote_info in sorted(self.remote.items()):
            local_info = self.local.get(name)
            if local_info is not None and local_info.rows != remote_info.rows:
                out.append(
                    {
                        "partition": name,
                        "local_rows": local_info.rows,
                        "remote_rows": remote_info.rows,
                        "delta": remote_info.rows - local_info.rows,
                    }
                )
        return out

    @property
    def local_only(self) -> list[str]:
        return sorted(name for name in self.local if name not in self.remote)


# ---------------------------------------------------------------------------
# ClickHouse transports
# ---------------------------------------------------------------------------


def _local_argv(container: str) -> list[str]:
    return ["docker", "exec", "-i", container, "clickhouse-client"]


def _remote_argv(remote: str, container: str) -> list[str]:
    # -C compresses the Native stream in transit. The remote command string is a fixed
    # literal and the SQL travels on stdin, so no query text is ever shell-quoted.
    return ["ssh", "-C", *SSH_OPTS, remote, f"docker exec -i {container} clickhouse-client"]


def _run_query(argv: Sequence[str], sql: str, *, timeout: int = 180) -> str:
    proc = subprocess.run(  # noqa: S603 - argv is built from validated config, never shell
        list(argv),
        input=sql.encode("utf-8"),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise SyncError(f"query failed (exit {proc.returncode}): {stderr[:500]}")
    return proc.stdout.decode("utf-8", "replace")


def _fetch_partitions(argv: Sequence[str], *, with_ttl: bool) -> dict[str, PartitionInfo]:
    """Inventory partitions from ``system.parts`` -- metadata only, zero data scanned."""
    ttl_select = ", max(delete_ttl_info_max) AS ttl" if with_ttl else ""
    sql = (
        f"SELECT partition, sum(rows) AS rows{ttl_select} "
        f"FROM system.parts WHERE database = '{DATABASE}' AND table = '{TABLE_NAME}' AND active "
        "GROUP BY partition ORDER BY partition FORMAT TSV"
    )
    out: dict[str, PartitionInfo] = {}
    for line in _run_query(argv, sql).splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        out[parts[0]] = PartitionInfo(
            partition=parts[0],
            rows=int(parts[1]),
            ttl_expiry=parts[2] if with_ttl and len(parts) > 2 else None,
        )
    return out


def _fetch_columns(argv: Sequence[str]) -> list[str]:
    sql = (
        f"SELECT name FROM system.columns WHERE database = '{DATABASE}' AND table = '{TABLE_NAME}' "
        "ORDER BY position FORMAT TSV"
    )
    return [line.strip() for line in _run_query(argv, sql).splitlines() if line.strip()]


def _partition_digest(argv: Sequence[str], partition: str) -> tuple[int, str]:
    sql = (
        f"SELECT count() AS rows, toString({ROW_DIGEST_EXPR}) AS digest "
        f"FROM {TABLE} WHERE {PARTITION_EXPR} = {int(partition)} FORMAT TSV"
    )
    line = _run_query(argv, sql, timeout=600).strip()
    if not line:
        return 0, "0"
    rows, digest = line.split("\t")
    return int(rows), digest


# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------


def _partial_write_warning(local_argv: Sequence[str], partition: str) -> str:
    """Describe any rows that landed locally before a transfer failed."""
    try:
        rows, _ = _partition_digest(local_argv, partition)
    except (SyncError, subprocess.TimeoutExpired):
        return f" -- could not determine whether rows landed in partition {partition}; inspect it manually."
    if rows == 0:
        return " -- no rows landed locally; the archive is unchanged."
    return (
        f" -- WARNING: {rows:,} rows landed locally and were NOT rolled back. "
        f"Run: ALTER TABLE {TABLE} DROP PARTITION '{partition}'  before retrying."
    )


def transfer_partition(
    *,
    partition: str,
    columns: Sequence[str],
    remote_argv: Sequence[str],
    local_argv: Sequence[str],
) -> dict[str, Any]:
    """Stream one partition remote -> local, verifying the digest on both sides.

    Refuses to write when the partition already holds local rows: a plain MergeTree
    INSERT duplicates rather than replaces.
    """
    remote_rows, remote_digest = _partition_digest(remote_argv, partition)
    if remote_rows == 0:
        raise SyncError(f"partition {partition} holds no rows on the remote; nothing to transfer")

    local_rows_before, _ = _partition_digest(local_argv, partition)
    if local_rows_before != 0:
        raise SyncError(
            f"refusing partition {partition}: {local_rows_before:,} rows already present locally. "
            "market_data is a plain MergeTree, so re-inserting duplicates rather than replaces."
        )

    column_list = ", ".join(columns)
    select_sql = f"SELECT {column_list} FROM {TABLE} WHERE {PARTITION_EXPR} = {int(partition)} FORMAT Native"
    insert_sql = f"INSERT INTO {TABLE} ({column_list}) FORMAT Native"

    remote = subprocess.Popen(  # noqa: S603 - fixed argv, SQL passed on stdin
        list(remote_argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    local = subprocess.Popen(  # noqa: S603 - fixed argv plus a query built from system.columns
        [*local_argv, "--query", insert_sql],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert remote.stdin is not None and remote.stdout is not None  # noqa: S101 - PIPE guaranteed
    assert local.stdin is not None  # noqa: S101 - PIPE guaranteed

    remote.stdin.write(select_sql.encode("utf-8"))
    remote.stdin.close()
    try:
        shutil.copyfileobj(remote.stdout, local.stdin, COPY_CHUNK_BYTES)
    finally:
        local.stdin.close()

    remote_err = remote.stderr.read().decode("utf-8", "replace") if remote.stderr else ""
    local_err = local.stderr.read().decode("utf-8", "replace") if local.stderr else ""
    remote.wait(timeout=1800)
    local.wait(timeout=1800)
    if remote.returncode != 0 or local.returncode != 0:
        # A remote failure mid-stream looks like a clean EOF to the local INSERT, which
        # then commits a *partial* partition and exits 0. Never report a transfer failure
        # without first saying whether rows landed.
        which = "remote SELECT" if remote.returncode != 0 else "local INSERT"
        code = remote.returncode if remote.returncode != 0 else local.returncode
        detail = (remote_err if remote.returncode != 0 else local_err)[:500]
        raise SyncError(
            f"{which} failed for {partition} (exit {code}): {detail}"
            f"{_partial_write_warning(local_argv, partition)}"
        )

    local_rows_after, local_digest = _partition_digest(local_argv, partition)
    verified = local_rows_after == remote_rows and local_digest == remote_digest
    result = {
        "partition": partition,
        "remote_rows": remote_rows,
        "local_rows": local_rows_after,
        "remote_digest": remote_digest,
        "local_digest": local_digest,
        "verified": verified,
    }
    if not verified:
        # Never auto-drop: dropping a partition is destructive and is the operator's call.
        result["remediation"] = (
            f"ALTER TABLE {TABLE} DROP PARTITION '{partition}'  -- then re-run this script"
        )
        raise SyncError(
            f"verification FAILED for {partition}: local {local_rows_after:,}/{local_digest} != "
            f"remote {remote_rows:,}/{remote_digest}. The partition was NOT rolled back. "
            f"Run: {result['remediation']}"
        )
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def build_inventory_payload(inventory: Inventory, *, remote_label: str) -> dict[str, Any]:
    """Reference inventory consumed by ``quality.evaluate_archive_sync``."""
    return {
        "schema": "hft_archive_inventory.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source": TABLE,
        "remote": remote_label,
        "partitions": [
            {
                "partition": info.partition,
                "rows": info.rows,
                "ttl_expiry": info.ttl_expiry,
            }
            for info in sorted(inventory.remote.values(), key=lambda i: i.partition)
        ],
    }


def render_diff(inventory: Inventory) -> str:
    lines = [
        f"local partitions : {len(inventory.local)}  ({sum(i.rows for i in inventory.local.values()):,} rows)",
        f"remote partitions: {len(inventory.remote)}  ({sum(i.rows for i in inventory.remote.values()):,} rows)",
        "",
    ]
    missing = inventory.missing_local
    if missing:
        lines.append(f"MISSING LOCALLY ({len(missing)}):")
        for info in missing:
            days = info.days_until_expiry
            horizon = "expiry unknown" if days is None else f"{days} days until remote TTL"
            urgency = " [URGENT]" if days is not None and days <= URGENT_HORIZON_DAYS else ""
            lines.append(f"  {info.partition}  {info.rows:>12,} rows   {horizon}{urgency}")
    else:
        lines.append("MISSING LOCALLY: none")
    deltas = inventory.row_deltas
    lines.append("")
    if deltas:
        lines.append(f"ROW-COUNT DELTAS ({len(deltas)}) -- NOT repairable by re-pulling (would duplicate):")
        for entry in deltas:
            lines.append(
                f"  {entry['partition']}  local {entry['local_rows']:,} vs remote "
                f"{entry['remote_rows']:,}  (delta {entry['delta']:+,})"
            )
    else:
        lines.append("ROW-COUNT DELTAS: none -- every shared partition matches exactly")
    if inventory.local_only:
        lines += [
            "",
            f"LOCAL-ONLY ({len(inventory.local_only)}): history already aged out of the remote "
            f"({inventory.local_only[0]}..{inventory.local_only[-1]})",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_market_data_archive",
        description="Pull missing hft.market_data partitions from production into the local archive.",
    )
    parser.add_argument(
        "--remote",
        default=None,
        help=f"ssh target for the production host (default: ${REMOTE_ENV_VAR} or .env)",
    )
    parser.add_argument("--remote-container", default="clickhouse")
    parser.add_argument("--local-container", default="clickhouse")
    parser.add_argument(
        "--partitions",
        default=None,
        help="Comma-separated partitions to transfer (default: every missing partition)",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Actually transfer. Without this flag the script only reports the diff.",
    )
    parser.add_argument(
        "--include-today",
        action="store_true",
        help="Do not skip today's partition. Unsafe: it is still being written.",
    )
    parser.add_argument("--emit-inventory", default=None, help="Write the remote inventory JSON here")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    remote = args.remote or _env_or_dotenv(REMOTE_ENV_VAR)
    if not remote:
        print(
            f"error: no production host configured. Set {REMOTE_ENV_VAR} in the environment "
            "or .env, or pass --remote user@host.",
            file=sys.stderr,
        )
        return 2

    local_argv = _local_argv(args.local_container)
    remote_argv = _remote_argv(remote, args.remote_container)

    try:
        inventory = Inventory(
            local=_fetch_partitions(local_argv, with_ttl=False),
            remote=_fetch_partitions(remote_argv, with_ttl=True),
        )
        local_columns = _fetch_columns(local_argv)
        remote_columns = _fetch_columns(remote_argv)
    except (SyncError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if set(local_columns) != set(remote_columns):
        only_local = sorted(set(local_columns) - set(remote_columns))
        only_remote = sorted(set(remote_columns) - set(local_columns))
        print(
            f"error: column sets diverge. local-only={only_local} remote-only={only_remote}",
            file=sys.stderr,
        )
        return 1

    print(render_diff(inventory))

    if args.emit_inventory:
        path = Path(args.emit_inventory)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = build_inventory_payload(inventory, remote_label=args.remote_container)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"\ninventory written: {path}")

    today = datetime.now(UTC).strftime("%Y%m%d")
    if args.partitions:
        wanted = [p.strip() for p in args.partitions.split(",") if p.strip()]
    else:
        wanted = [info.partition for info in inventory.missing_local]

    skipped = [p for p in wanted if p == today and not args.include_today]
    wanted = [p for p in wanted if p not in skipped]
    if skipped:
        print(f"\nskipping in-flight partition(s) {skipped} (still being written; use --include-today to force)")

    if not args.sync:
        print(f"\nDRY RUN. {len(wanted)} partition(s) would transfer: {wanted or 'none'}")
        print("Re-run with --sync to write.")
        return 0
    if not wanted:
        print("\nnothing to transfer.")
        return 0

    print(f"\nsyncing {len(wanted)} partition(s) using column order from the local table...")
    results: list[dict[str, Any]] = []
    for partition in wanted:
        try:
            result = transfer_partition(
                partition=partition,
                columns=local_columns,
                remote_argv=remote_argv,
                local_argv=local_argv,
            )
        except (SyncError, subprocess.TimeoutExpired) as exc:
            print(f"  {partition}: FAILED -- {exc}", file=sys.stderr)
            print(json.dumps({"results": results}, indent=2, sort_keys=True))
            return 1
        results.append(result)
        print(f"  {partition}: verified {result['local_rows']:,} rows (digest {result['local_digest']})")

    print(json.dumps({"results": results}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
