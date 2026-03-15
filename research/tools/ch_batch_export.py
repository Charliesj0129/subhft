"""Batch export ClickHouse market data for alpha research + MM backtesting.

Exports from both ``hft.market_data`` (Int64 prices) and ``hft.market_data_backup``
(Float64 prices) tables, producing:

1. **L1 research .npy** — structured array for alpha feature precompute
   Fields: (bid_px, ask_px, bid_qty, ask_qty, mid_price, spread_bps, volume, local_ts)

2. **L2 hftbacktest .npz** — event-based format for MM backtesting with fill simulation

Usage::

    # Export all available dates for TXFC6 + 2330
    python research/tools/ch_batch_export.py \
        --symbols TXFC6,TXFB6,2330 \
        --host 100.91.176.126 \
        --formats l1,l2

    # Export specific date range
    python research/tools/ch_batch_export.py \
        --symbols TXFC6 \
        --host 100.91.176.126 \
        --date-from 2026-03-03 --date-to 2026-03-13

    # Dry-run: list what would be exported
    python research/tools/ch_batch_export.py \
        --symbols TXFC6 --host 100.91.176.126 --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from structlog import get_logger

logger = get_logger("ch_batch_export")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CH_PRICE_SCALE_INT: float = 1_000_000.0   # market_data: price_scaled (Int64) → float
CH_PRICE_SCALE_FLOAT: float = 1.0          # market_data_backup: price (Float64) already float

# L1 research dtype (compatible with feature_precompute.py)
L1_DTYPE = np.dtype([
    ("bid_px", "f8"),
    ("ask_px", "f8"),
    ("bid_qty", "f8"),
    ("ask_qty", "f8"),
    ("mid_price", "f8"),
    ("spread_bps", "f8"),
    ("volume", "f8"),
    ("local_ts", "i8"),
])

# hftbacktest event dtype
try:
    from hftbacktest.types import (
        BUY_EVENT,
        DEPTH_EVENT,
        DEPTH_SNAPSHOT_EVENT,
        EXCH_EVENT,
        LOCAL_EVENT,
        SELL_EVENT,
        TRADE_EVENT,
    )
    from hftbacktest.types import event_dtype as HBT_EVENT_DTYPE

    _HFTBT_AVAILABLE = True
except ImportError:
    _HFTBT_AVAILABLE = False
    HBT_EVENT_DTYPE = None

# De-dup threshold for L2 export
DEDUP_WINDOW_NS: int = 500_000  # 0.5 ms


# ---------------------------------------------------------------------------
# ClickHouse helpers
# ---------------------------------------------------------------------------

def _get_client(host: str, port: int = 8123, user: str = "default", password: str = ""):
    """Create ClickHouse client."""
    import clickhouse_connect
    return clickhouse_connect.get_client(
        host=host, port=port, username=user, password=password,
    )


def _discover_dates(
    client: Any,
    symbol: str,
    table: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[str]:
    """Discover available trading dates for a symbol."""
    where_parts = ["symbol = %(symbol)s"]
    params: dict[str, Any] = {"symbol": symbol}

    if date_from:
        where_parts.append("toDate(fromUnixTimestamp64Nano(ingest_ts)) >= %(date_from)s")
        params["date_from"] = date_from
    if date_to:
        where_parts.append("toDate(fromUnixTimestamp64Nano(ingest_ts)) <= %(date_to)s")
        params["date_to"] = date_to

    where = " AND ".join(where_parts)
    query = f"""
        SELECT
            toDate(fromUnixTimestamp64Nano(ingest_ts)) AS dt,
            count() AS rows
        FROM hft.{table}
        WHERE {where}
        GROUP BY dt
        HAVING rows > 100
        ORDER BY dt
    """
    result = client.query(query, parameters=params, settings={"max_memory_usage": 2_500_000_000})
    return [(str(row[0]), int(row[1])) for row in result.result_rows]


def _detect_table_for_symbol(client: Any, symbol: str) -> list[tuple[str, str, int]]:
    """Detect which tables have data for this symbol and return (table, date, rows)."""
    entries: list[tuple[str, str, int]] = []

    # Check market_data (current, Int64 prices)
    try:
        for dt, rows in _discover_dates(client, symbol, "market_data"):
            entries.append(("market_data", dt, rows))
    except Exception:
        pass

    # Check market_data_backup (old, Float64 prices)
    try:
        for dt, rows in _discover_dates(client, symbol, "market_data_backup"):
            entries.append(("market_data_backup", dt, rows))
    except Exception:
        pass

    # Deduplicate: prefer market_data over backup for same date
    seen_dates: dict[str, tuple[str, str, int]] = {}
    for table, dt, rows in entries:
        if dt not in seen_dates or table == "market_data":
            seen_dates[dt] = (table, dt, rows)

    return sorted(seen_dates.values(), key=lambda x: x[1])


# ---------------------------------------------------------------------------
# L1 Export (research .npy format)
# ---------------------------------------------------------------------------

def _export_l1_day(
    client: Any,
    symbol: str,
    date: str,
    table: str,
    out_dir: Path,
) -> str | None:
    """Export one day of L1 data as research .npy."""
    is_backup = table == "market_data_backup"
    price_col = "price" if is_backup else "price_scaled"
    price_scale = CH_PRICE_SCALE_FLOAT if is_backup else CH_PRICE_SCALE_INT

    # For L1, we only need BidAsk rows (L1 = best bid/ask)
    query = f"""
        SELECT
            ingest_ts,
            bids_price,
            asks_price,
            bids_vol,
            asks_vol,
            type,
            {price_col} AS px,
            volume
        FROM hft.{table}
        WHERE symbol = %(symbol)s
          AND toDate(fromUnixTimestamp64Nano(ingest_ts)) = %(date)s
        ORDER BY ingest_ts, seq_no
    """
    result = client.query(
        query,
        parameters={"symbol": symbol, "date": date},
        settings={"max_memory_usage": 2_500_000_000},
    )
    rows = result.result_rows
    if not rows:
        return None

    # Build L1 records from BidAsk rows
    records: list[tuple] = []
    for row in rows:
        ingest_ts, bids_price, asks_price, bids_vol, asks_vol, row_type, px, vol = row

        if row_type != "BidAsk":
            continue

        if not bids_price or not asks_price or not bids_vol or not asks_vol:
            continue

        bp_raw = bids_price[0]
        ap_raw = asks_price[0]
        bq = float(bids_vol[0])
        aq = float(asks_vol[0])

        bp = float(bp_raw) / price_scale
        ap = float(ap_raw) / price_scale

        if bp <= 0 or ap <= 0 or ap <= bp:
            continue

        mid = (bp + ap) / 2.0
        spread_bps = (ap - bp) / mid * 10000.0

        records.append((bp, ap, bq, aq, mid, spread_bps, 0.0, int(ingest_ts)))

    if not records:
        return None

    arr = np.array(records, dtype=L1_DTYPE)

    out_path = out_dir / f"{symbol}_{date}_l1.npy"
    np.save(str(out_path), arr)

    # Meta
    digest = hashlib.sha256(arr.tobytes()[:4096]).hexdigest()
    meta = {
        "dataset_id": f"{symbol}_{date}_l1",
        "source_type": "real",
        "source": f"clickhouse_hft.{table}",
        "generator": "ch_batch_export",
        "schema_version": 1,
        "rows": len(arr),
        "fields": list(arr.dtype.names),
        "symbols": [symbol],
        "date": date,
        "data_fingerprint": digest,
        "data_ul": 5,
        "price_scale_applied": price_scale,
        "created_at": datetime.now(UTC).isoformat(),
    }
    meta_path = out_dir / f"{symbol}_{date}_l1.npy.meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    logger.info("l1_exported", symbol=symbol, date=date, rows=len(arr), path=str(out_path))
    return str(out_path)


# ---------------------------------------------------------------------------
# L2 Export (hftbacktest .npz format)
# ---------------------------------------------------------------------------

def _build_hbt_event(ev: int, exch_ts: int, local_ts: int, px: float, qty: float) -> tuple:
    """Build an hftbacktest event tuple."""
    return (ev, int(exch_ts), int(local_ts), float(px), float(qty), 0, 0, 0.0)


def _export_l2_day(
    client: Any,
    symbol: str,
    date: str,
    table: str,
    out_dir: Path,
) -> str | None:
    """Export one day of L2 data as hftbacktest .npz."""
    if not _HFTBT_AVAILABLE:
        logger.warning("hftbacktest_not_installed", msg="Skipping L2 export")
        return None

    is_backup = table == "market_data_backup"
    price_col = "price" if is_backup else "price_scaled"
    price_scale = CH_PRICE_SCALE_FLOAT if is_backup else CH_PRICE_SCALE_INT

    query = f"""
        SELECT
            type,
            ingest_ts,
            bids_price,
            asks_price,
            bids_vol,
            asks_vol,
            {price_col} AS px,
            volume
        FROM hft.{table}
        WHERE symbol = %(symbol)s
          AND toDate(fromUnixTimestamp64Nano(ingest_ts)) = %(date)s
        ORDER BY ingest_ts, seq_no
    """
    result = client.query(
        query,
        parameters={"symbol": symbol, "date": date},
        settings={"max_memory_usage": 2_500_000_000},
    )
    rows_raw = result.result_rows
    if not rows_raw:
        return None

    # De-dup consecutive BidAsk
    deduped = []
    prev_key: tuple | None = None
    prev_ts: int = 0
    dedup_count = 0

    for row in rows_raw:
        row_type, ts, bids_price, asks_price, bids_vol, asks_vol, px, vol = row
        if row_type == "BidAsk":
            key = (tuple(bids_price or []), tuple(asks_price or []))
            ts_int = int(ts)
            if key == prev_key and (ts_int - prev_ts) < DEDUP_WINDOW_NS:
                dedup_count += 1
                continue
            prev_key = key
            prev_ts = ts_int
        deduped.append(row)

    # Build events
    events: list[tuple] = []
    snapshot_written = False

    for row in deduped:
        row_type, ts, bids_price, asks_price, bids_vol, asks_vol, px, vol = row
        ts_int = int(ts)

        if row_type == "BidAsk":
            bp_list = bids_price or []
            ap_list = asks_price or []
            bv_list = bids_vol or []
            av_list = asks_vol or []
            n_levels = min(len(bp_list), len(ap_list), len(bv_list), len(av_list))
            if n_levels == 0:
                continue

            for i in range(n_levels):
                bp = float(bp_list[i]) / price_scale
                bq = float(bv_list[i]) if i < len(bv_list) else 0.0
                ap = float(ap_list[i]) / price_scale
                aq = float(av_list[i]) if i < len(av_list) else 0.0

                if bp > 0:
                    if not snapshot_written:
                        ev = int(DEPTH_SNAPSHOT_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT)
                    else:
                        ev = int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT)
                    events.append(_build_hbt_event(ev, ts_int, ts_int, bp, bq))

                if ap > 0:
                    if not snapshot_written:
                        ev = int(DEPTH_SNAPSHOT_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT)
                    else:
                        ev = int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT)
                    events.append(_build_hbt_event(ev, ts_int, ts_int, ap, aq))

            if not snapshot_written and n_levels > 0:
                snapshot_written = True

        elif row_type == "Tick" and snapshot_written:
            price_raw = px if px else 0
            vol_f = float(vol or 0)
            if price_raw > 0 and vol_f > 0:
                price = float(price_raw) / price_scale
                ev = int(TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT)
                events.append(_build_hbt_event(ev, ts_int, ts_int, price, vol_f))

    if not events:
        return None

    event_arr = np.array(events, dtype=HBT_EVENT_DTYPE)
    out_path = out_dir / f"{symbol}_{date}_l2.hftbt.npz"
    np.savez_compressed(str(out_path), data=event_arr)

    # Meta
    digest = hashlib.sha256(event_arr.tobytes()[:4096]).hexdigest()
    meta = {
        "dataset_id": f"{symbol}_{date}_l2",
        "source_type": "real",
        "source": f"clickhouse_hft.{table}",
        "generator": "ch_batch_export",
        "schema_version": 1,
        "rows": len(event_arr),
        "fields": list(event_arr.dtype.names or ()),
        "symbols": [symbol],
        "date": date,
        "depth_levels": 5,
        "data_fingerprint": digest,
        "data_ul": 5,
        "dedup_removed": dedup_count,
        "price_scale_applied": price_scale,
        "created_at": datetime.now(UTC).isoformat(),
    }
    meta_path = out_dir / f"{symbol}_{date}_l2.hftbt.npz.meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    logger.info("l2_exported", symbol=symbol, date=date, events=len(event_arr), path=str(out_path))
    return str(out_path)


# ---------------------------------------------------------------------------
# Batch orchestration
# ---------------------------------------------------------------------------

def run_batch_export(
    *,
    symbols: list[str],
    host: str = "100.91.176.126",
    port: int = 8123,
    user: str = "default",
    password: str = "",
    formats: list[str] = ("l1",),
    out_base: str = "research/data/raw",
    date_from: str | None = None,
    date_to: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run batch export for multiple symbols across all available dates.

    Returns summary dict with export statistics.
    """
    client = _get_client(host, port, user, password)
    out_base_path = Path(out_base)
    out_base_path.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "symbols": {},
        "total_l1_files": 0,
        "total_l2_files": 0,
        "total_l1_rows": 0,
        "errors": [],
    }

    for symbol in symbols:
        logger.info("discovering", symbol=symbol)
        entries = _detect_table_for_symbol(client, symbol)

        # Apply date filters
        if date_from:
            entries = [(t, d, r) for t, d, r in entries if d >= date_from]
        if date_to:
            entries = [(t, d, r) for t, d, r in entries if d <= date_to]

        sym_dir = out_base_path / symbol.lower()
        sym_dir.mkdir(parents=True, exist_ok=True)

        sym_summary = {
            "dates": len(entries),
            "total_rows_in_ch": sum(r for _, _, r in entries),
            "l1_files": [],
            "l2_files": [],
        }

        if dry_run:
            logger.info(
                "dry_run",
                symbol=symbol,
                dates=len(entries),
                date_range=f"{entries[0][1]}..{entries[-1][1]}" if entries else "none",
                total_rows=sym_summary["total_rows_in_ch"],
            )
            for table, dt, rows in entries:
                logger.info("  would_export", table=table, date=dt, rows=rows)
            summary["symbols"][symbol] = sym_summary
            continue

        for table, dt, rows in entries:
            logger.info("exporting", symbol=symbol, date=dt, table=table, rows=rows)

            if "l1" in formats:
                # Skip if already exported
                l1_path = sym_dir / f"{symbol}_{dt}_l1.npy"
                if l1_path.exists():
                    logger.info("l1_skipped", path=str(l1_path), reason="already_exists")
                    sym_summary["l1_files"].append(str(l1_path))
                else:
                    try:
                        path = _export_l1_day(client, symbol, dt, table, sym_dir)
                        if path:
                            sym_summary["l1_files"].append(path)
                    except Exception as e:
                        logger.error("l1_export_failed", symbol=symbol, date=dt, error=str(e))
                        summary["errors"].append(f"L1 {symbol}/{dt}: {e}")

            if "l2" in formats:
                l2_path = sym_dir / f"{symbol}_{dt}_l2.hftbt.npz"
                if l2_path.exists():
                    logger.info("l2_skipped", path=str(l2_path), reason="already_exists")
                    sym_summary["l2_files"].append(str(l2_path))
                else:
                    try:
                        path = _export_l2_day(client, symbol, dt, table, sym_dir)
                        if path:
                            sym_summary["l2_files"].append(path)
                    except Exception as e:
                        logger.error("l2_export_failed", symbol=symbol, date=dt, error=str(e))
                        summary["errors"].append(f"L2 {symbol}/{dt}: {e}")

        summary["symbols"][symbol] = sym_summary
        summary["total_l1_files"] += len(sym_summary["l1_files"])
        summary["total_l2_files"] += len(sym_summary["l2_files"])

    return summary


def concat_l1_files(
    symbol_dir: str | Path,
    symbol: str,
    out_path: str | Path | None = None,
) -> str:
    """Concatenate per-day L1 .npy files into a single multi-day .npy.

    This is the format expected by ``feature_precompute.py``.
    """
    sym_dir = Path(symbol_dir)
    files = sorted(sym_dir.glob(f"{symbol}_*_l1.npy"))
    if not files:
        raise FileNotFoundError(f"No L1 files for {symbol} in {sym_dir}")

    arrays = []
    for f in files:
        arr = np.load(str(f))
        arrays.append(arr)
        logger.info("concat_loaded", file=f.name, rows=len(arr))

    combined = np.concatenate(arrays)

    if out_path is None:
        out_path = sym_dir / f"{symbol}_all_l1.npy"
    else:
        out_path = Path(out_path)

    np.save(str(out_path), combined)

    # Meta
    digest = hashlib.sha256(combined.tobytes()[:4096]).hexdigest()
    meta = {
        "dataset_id": f"{symbol}_all_l1",
        "source_type": "real",
        "generator": "ch_batch_export.concat_l1_files",
        "rows": len(combined),
        "fields": list(combined.dtype.names),
        "symbols": [symbol],
        "source_files": [f.name for f in files],
        "date_range": f"{files[0].stem.split('_')[1]}..{files[-1].stem.split('_')[1]}",
        "data_fingerprint": digest,
        "data_ul": 5,
        "created_at": datetime.now(UTC).isoformat(),
    }
    meta_path = Path(str(out_path) + ".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    logger.info("concat_complete", symbol=symbol, total_rows=len(combined), path=str(out_path))
    return str(out_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch export ClickHouse market data for alpha research + MM backtesting."
    )
    parser.add_argument(
        "--symbols", required=True,
        help="Comma-separated symbols (e.g., TXFC6,TXFB6,2330)",
    )
    parser.add_argument("--host", default="100.91.176.126", help="ClickHouse host")
    parser.add_argument("--port", type=int, default=8123, help="ClickHouse HTTP port")
    parser.add_argument("--user", default="default", help="ClickHouse user")
    parser.add_argument("--password", default="", help="ClickHouse password")
    parser.add_argument(
        "--formats", default="l1",
        help="Comma-separated: l1 (research .npy), l2 (hftbacktest .npz)",
    )
    parser.add_argument("--out", default="research/data/raw", help="Output base directory")
    parser.add_argument("--date-from", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--date-to", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="List dates without exporting")
    parser.add_argument(
        "--concat", action="store_true",
        help="After export, concatenate per-day L1 files into single .npy per symbol",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    symbols = [s.strip() for s in args.symbols.split(",")]
    formats = [f.strip() for f in args.formats.split(",")]

    summary = run_batch_export(
        symbols=symbols,
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        formats=formats,
        out_base=args.out,
        date_from=args.date_from,
        date_to=args.date_to,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        logger.info("dry_run_complete", summary=summary)
        return 0

    # Concat L1 if requested
    if args.concat and "l1" in formats:
        for symbol in symbols:
            sym_dir = Path(args.out) / symbol.lower()
            try:
                concat_l1_files(sym_dir, symbol)
            except FileNotFoundError as e:
                logger.warning("concat_skipped", symbol=symbol, reason=str(e))

    # Print summary
    logger.info("batch_export_complete", summary=summary)
    if summary["errors"]:
        logger.warning("errors_occurred", count=len(summary["errors"]))
        for err in summary["errors"]:
            logger.error("export_error", detail=err)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
