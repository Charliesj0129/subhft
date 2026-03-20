#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"


def _ch_query(clickhouse_url: str, query: str, password: str = "") -> tuple[str | None, str | None]:
    """Execute a ClickHouse HTTP query. Returns (result_text, error_text)."""
    try:
        params: dict[str, str] = {"query": query}
        if password:
            params["password"] = password
        url = clickhouse_url.rstrip("/") + "/?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8").strip()
        return body, None
    except Exception as exc:
        return None, str(exc)


def _check_wal_files(wal_dir: str) -> tuple[str, str]:
    """Count files and total size in WAL directory. WARN if > 100 files."""
    wal_path = Path(wal_dir)
    if not wal_path.exists():
        return STATUS_PASS, "WAL directory does not exist (no backlog)"

    total_bytes = 0
    file_count = 0
    try:
        for f in wal_path.rglob("*"):
            if f.is_file():
                total_bytes += f.stat().st_size
                file_count += 1
    except Exception as exc:
        return STATUS_FAIL, f"error scanning WAL dir: {exc}"

    total_mb = total_bytes / (1024 * 1024)
    detail = f"{file_count} files, {total_mb:.1f} MB"

    if file_count > 100:
        return STATUS_WARN, f"{detail} (>100 files pending)"
    return STATUS_PASS, detail


def _check_ch_record_count(clickhouse_url: str, password: str) -> tuple[str, str]:
    """Query today's market_data record count from ClickHouse."""
    query = "SELECT count() FROM hft.market_data WHERE toDate(exch_ts / 1000000000) = today()"
    result, err = _ch_query(clickhouse_url, query, password)
    if err is not None:
        return STATUS_FAIL, f"ClickHouse query failed: {err}"

    try:
        count = int(result)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return STATUS_FAIL, f"unexpected response: {result!r}"

    today = dt.date.today()
    is_weekday = today.weekday() < 5

    if count == 0 and is_weekday:
        return STATUS_WARN, f"0 records today ({today.isoformat()}, weekday -- expected data)"
    return STATUS_PASS, f"{count:,} records today ({today.isoformat()})"


def _check_daily_pnl(clickhouse_url: str, password: str) -> tuple[str, str]:
    """Query today's fill-based PnL summary from ClickHouse."""
    query = "SELECT sum(price * qty) FROM hft.fills WHERE toDate(match_ts_ns / 1000000000) = today()"
    result, err = _ch_query(clickhouse_url, query, password)
    if err is not None:
        return STATUS_WARN, f"PnL query failed: {err}"

    try:
        value = int(result)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        if result in {"", "0"}:
            value = 0
        else:
            return STATUS_WARN, f"unexpected PnL response: {result!r}"

    # PnL is scaled x10000, convert for display
    pnl_display = value / 10000.0
    return STATUS_PASS, f"daily fill turnover (scaled): {value:,} (approx {pnl_display:,.2f})"


def _check_wal_backlog(wal_dir: str) -> tuple[str, str]:
    """If WAL files exist, warn that loader should drain them."""
    wal_path = Path(wal_dir)
    if not wal_path.exists():
        return STATUS_PASS, "no WAL directory"

    file_count = 0
    try:
        for f in wal_path.rglob("*"):
            if f.is_file():
                file_count += 1
    except Exception as exc:
        return STATUS_FAIL, f"error scanning WAL dir: {exc}"

    if file_count > 0:
        return STATUS_WARN, f"{file_count} WAL files pending -- run WAL loader to drain backlog"
    return STATUS_PASS, "WAL directory empty (no backlog)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post-market daily check for HFT platform")
    parser.add_argument("--wal-dir", default=".wal", help="WAL directory path (default: .wal)")
    parser.add_argument(
        "--clickhouse-url",
        default="http://localhost:8123",
        help="ClickHouse HTTP URL",
    )
    parser.add_argument(
        "--clickhouse-password",
        default=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        help="ClickHouse password (default: from env CLICKHOUSE_PASSWORD)",
    )
    args = parser.parse_args(argv)

    checks: list[tuple[str, str, str]] = []

    check_fns: list[tuple[str, object]] = [
        ("WAL file count/size", lambda: _check_wal_files(args.wal_dir)),
        ("CH record count today", lambda: _check_ch_record_count(args.clickhouse_url, args.clickhouse_password)),
        ("Daily PnL summary", lambda: _check_daily_pnl(args.clickhouse_url, args.clickhouse_password)),
        ("WAL backlog check", lambda: _check_wal_backlog(args.wal_dir)),
    ]

    for name, fn in check_fns:
        try:
            status, detail = fn()  # type: ignore[operator]
        except Exception as exc:
            status, detail = STATUS_FAIL, f"unhandled error: {exc}"
        checks.append((name, status, detail))

    # Print summary table
    print("")
    print("=" * 72)
    print("  POST-MARKET CHECK SUMMARY")
    print("=" * 72)
    print(f"  {'Check':<25} {'Status':<8} {'Detail'}")
    print("-" * 72)
    has_fail = False
    for name, status, detail in checks:
        tag = status.upper()
        print(f"  {name:<25} {tag:<8} {detail}")
        if status == STATUS_FAIL:
            has_fail = True
    print("-" * 72)

    pass_count = sum(1 for _, s, _ in checks if s == STATUS_PASS)
    warn_count = sum(1 for _, s, _ in checks if s == STATUS_WARN)
    fail_count = sum(1 for _, s, _ in checks if s == STATUS_FAIL)
    print(f"  Total: {len(checks)} checks | pass={pass_count} warn={warn_count} fail={fail_count}")
    print("=" * 72)
    print("")

    if has_fail:
        print("RESULT: FAIL -- investigate issues")
        return 1
    if warn_count > 0:
        print("RESULT: WARN -- review warnings")
    else:
        print("RESULT: PASS -- all post-market checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
