#!/usr/bin/env python3
"""Export L2 (5-level depth) market data from ClickHouse → hftbacktest .npz format.

Usage:
    python research/tools/ch_l2_export.py \
        --symbol TXFC6 --date 2026-03-06 \
        --host 100.91.176.126 \
        --out research/data/processed/TXFC6/

Reads from ``hft.market_data`` table, de-duplicates Shioaji double-callbacks,
converts BidAsk rows to 10 DEPTH_EVENTs (5 bid + 5 ask levels) and Tick rows
to TRADE_EVENTs, then writes a compressed ``.npz`` with a UL5 ``.meta.json``
sidecar.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# hftbacktest event dtype & flags (with fallback if library unavailable)
# ---------------------------------------------------------------------------

CLICKHOUSE_PRICE_SCALE: float = 1_000_000.0
"""ClickHouse stores prices as Int64 scaled ×1,000,000."""

DEPTH_LEVELS: int = 5
"""Number of bid/ask depth levels per BidAsk row."""

DEDUP_WINDOW_NS: int = 500_000
"""0.5 ms dedup window for consecutive identical BidAsk rows."""


def _build_event_dtype() -> np.dtype:
    """Return the hftbacktest event dtype."""
    try:
        from hftbacktest.types import event_dtype  # type: ignore[import-untyped]

        return event_dtype
    except ImportError:
        return np.dtype(
            [
                ("ev", "<u8"),
                ("exch_ts", "<i8"),
                ("local_ts", "<i8"),
                ("px", "<f8"),
                ("qty", "<f8"),
            ]
        )


def _event_flags() -> dict[str, int]:
    """Get hftbacktest event flag constants."""
    try:
        from hftbacktest.types import (  # type: ignore[import-untyped]
            BUY_EVENT,
            DEPTH_EVENT,
            DEPTH_SNAPSHOT_EVENT,
            EXCH_EVENT,
            LOCAL_EVENT,
            SELL_EVENT,
            TRADE_EVENT,
        )

        return {
            "DEPTH_SNAPSHOT_BID": int(
                DEPTH_SNAPSHOT_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT
            ),
            "DEPTH_SNAPSHOT_ASK": int(
                DEPTH_SNAPSHOT_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT
            ),
            "DEPTH_BID": int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT),
            "DEPTH_ASK": int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT),
            "TRADE": int(TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT),
        }
    except ImportError:
        EXCH = 1 << 31
        LOCAL = 1 << 30
        BUY = 1 << 29
        SELL = 1 << 28
        DEPTH = 1 << 21
        TRADE = 1 << 20
        SNAPSHOT = 1 << 19
        return {
            "DEPTH_SNAPSHOT_BID": SNAPSHOT | DEPTH | EXCH | LOCAL | BUY,
            "DEPTH_SNAPSHOT_ASK": SNAPSHOT | DEPTH | EXCH | LOCAL | SELL,
            "DEPTH_BID": DEPTH | EXCH | LOCAL | BUY,
            "DEPTH_ASK": DEPTH | EXCH | LOCAL | SELL,
            "TRADE": TRADE | EXCH | LOCAL,
        }


# ---------------------------------------------------------------------------
# Core conversion helpers
# ---------------------------------------------------------------------------


def _arrays_equal(a: list[int], b: list[int]) -> bool:
    """Equality check for two int lists."""
    return a == b


def _dedup_bidask(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Remove consecutive BidAsk rows with identical depth within *DEDUP_WINDOW_NS*.

    Returns the deduplicated list and the count of removed rows.
    """
    if not rows:
        return rows, 0

    kept: list[dict[str, Any]] = [rows[0]]
    removed = 0

    for i in range(1, len(rows)):
        cur = rows[i]
        prev = kept[-1]

        ts_diff = cur["ingest_ts"] - prev["ingest_ts"]
        if (
            ts_diff < DEDUP_WINDOW_NS
            and _arrays_equal(cur["bids_price"], prev["bids_price"])
            and _arrays_equal(cur["asks_price"], prev["asks_price"])
        ):
            removed += 1
            continue

        kept.append(cur)

    return kept, removed


def _bidask_to_depth_events(
    row: dict[str, Any],
    *,
    is_snapshot: bool,
    flags: dict[str, int],
    evt_dtype: np.dtype,
) -> np.ndarray:
    """Convert one BidAsk row into up to 10 DEPTH_EVENTs (5 bid + 5 ask).

    Levels with ``price == 0`` are skipped.
    """
    bid_flag = flags["DEPTH_SNAPSHOT_BID"] if is_snapshot else flags["DEPTH_BID"]
    ask_flag = flags["DEPTH_SNAPSHOT_ASK"] if is_snapshot else flags["DEPTH_ASK"]
    ts_exch = row["ingest_ts"]
    ts_local = row["ingest_ts"]

    bids_price = row["bids_price"]
    bids_vol = row["bids_vol"]
    asks_price = row["asks_price"]
    asks_vol = row["asks_vol"]

    buf = np.zeros(DEPTH_LEVELS * 2, dtype=evt_dtype)
    idx = 0

    for lvl in range(min(len(bids_price), DEPTH_LEVELS)):
        px_raw = bids_price[lvl]
        if px_raw == 0:
            continue
        buf[idx]["ev"] = bid_flag
        buf[idx]["exch_ts"] = ts_exch
        buf[idx]["local_ts"] = ts_local
        buf[idx]["px"] = float(px_raw) / CLICKHOUSE_PRICE_SCALE
        buf[idx]["qty"] = float(bids_vol[lvl]) if lvl < len(bids_vol) else 0.0
        idx += 1

    for lvl in range(min(len(asks_price), DEPTH_LEVELS)):
        px_raw = asks_price[lvl]
        if px_raw == 0:
            continue
        buf[idx]["ev"] = ask_flag
        buf[idx]["exch_ts"] = ts_exch
        buf[idx]["local_ts"] = ts_local
        buf[idx]["px"] = float(px_raw) / CLICKHOUSE_PRICE_SCALE
        buf[idx]["qty"] = float(asks_vol[lvl]) if lvl < len(asks_vol) else 0.0
        idx += 1

    return buf[:idx]


def _tick_to_trade_event(
    row: dict[str, Any],
    *,
    flags: dict[str, int],
    evt_dtype: np.dtype,
) -> np.ndarray:
    """Convert one Tick row into a single TRADE_EVENT."""
    px = float(row["price_scaled"]) / CLICKHOUSE_PRICE_SCALE
    qty = float(row["volume"])
    ts = row["ingest_ts"]
    event = np.zeros(1, dtype=evt_dtype)
    event[0]["ev"] = flags["TRADE"]
    event[0]["exch_ts"] = ts
    event[0]["local_ts"] = ts
    event[0]["px"] = px
    event[0]["qty"] = qty
    return event


# ---------------------------------------------------------------------------
# ClickHouse query
# ---------------------------------------------------------------------------

_QUERY = """\
SELECT symbol, type, ingest_ts, price_scaled, volume,
       bids_price, bids_vol, asks_price, asks_vol, seq_no
FROM hft.market_data
WHERE symbol = {symbol:String}
  AND toYYYYMMDD(toDateTime(ingest_ts / 1000000000)) = {date_int:UInt32}
ORDER BY ingest_ts, seq_no
"""


def _fetch_rows(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    symbol: str,
    date_int: int,
    max_memory: int,
) -> list[dict[str, Any]]:
    """Fetch L2 rows from ClickHouse via ``clickhouse_connect``."""
    import clickhouse_connect  # type: ignore[import-untyped]

    client = clickhouse_connect.get_client(
        host=host,
        port=port,
        username=user,
        password=password,
        settings={"max_memory_usage": max_memory},
    )

    log.info("querying_clickhouse", symbol=symbol, date_int=date_int, host=host)
    result = client.query(
        _QUERY,
        parameters={"symbol": symbol, "date_int": date_int},
    )

    columns = result.column_names
    rows: list[dict[str, Any]] = []
    for row_tuple in result.result_rows:
        rows.append(dict(zip(columns, row_tuple)))

    log.info("fetched_rows", count=len(rows))
    return rows


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def convert_rows_to_events(
    rows: list[dict[str, Any]],
) -> tuple[np.ndarray, dict[str, int]]:
    """Convert raw ClickHouse rows into hftbacktest event array.

    Returns ``(events_array, stats_dict)``.
    """
    evt_dtype = _build_event_dtype()
    flags = _event_flags()

    bidask_rows = [r for r in rows if r["type"] == "BidAsk"]
    tick_rows = [r for r in rows if r["type"] == "Tick"]

    original_bidask = len(bidask_rows)
    original_tick = len(tick_rows)

    bidask_rows, dedup_removed = _dedup_bidask(bidask_rows)

    log.info(
        "dedup_complete",
        original=original_bidask,
        kept=len(bidask_rows),
        removed=dedup_removed,
    )

    # Merge and sort all events by (ingest_ts, seq_no)
    all_rows: list[tuple[int, int, str, dict[str, Any]]] = []
    for r in bidask_rows:
        all_rows.append((r["ingest_ts"], r["seq_no"], "BidAsk", r))
    for r in tick_rows:
        all_rows.append((r["ingest_ts"], r["seq_no"], "Tick", r))
    all_rows.sort(key=lambda x: (x[0], x[1]))

    # Pre-allocate: max events = bidask * 10 + ticks * 1
    max_events = len(bidask_rows) * DEPTH_LEVELS * 2 + len(tick_rows)
    events = np.zeros(max_events, dtype=evt_dtype)
    idx = 0
    first_bidask_seen = False

    for _, _, row_type, row in all_rows:
        if row_type == "BidAsk":
            is_snapshot = not first_bidask_seen
            first_bidask_seen = True
            depth_events = _bidask_to_depth_events(
                row, is_snapshot=is_snapshot, flags=flags, evt_dtype=evt_dtype
            )
            n = len(depth_events)
            if n > 0:
                events[idx : idx + n] = depth_events
                idx += n
        else:
            trade_event = _tick_to_trade_event(row, flags=flags, evt_dtype=evt_dtype)
            events[idx] = trade_event[0]
            idx += 1

    events = events[:idx]

    stats = {
        "original_bidask_rows": original_bidask,
        "original_tick_rows": original_tick,
        "dedup_removed": dedup_removed,
        "total_events": idx,
    }
    return events, stats


def write_npz(events: np.ndarray, out_path: Path) -> None:
    """Write events to compressed ``.npz``."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(str(out_path), data=events)
    log.info("wrote_npz", path=str(out_path), events=len(events))


def write_meta_json(
    npz_path: Path,
    *,
    symbol: str,
    date_str: str,
    stats: dict[str, int],
) -> Path:
    """Write UL5 ``.meta.json`` sidecar."""
    raw_bytes = npz_path.read_bytes()
    fingerprint = hashlib.sha256(raw_bytes[:1024]).hexdigest()

    meta: dict[str, Any] = {
        "dataset_id": f"{symbol}_{date_str.replace('-', '')}",
        "source_type": "real",
        "owner": "research-team",
        "schema_version": 1,
        "rows": stats["total_events"],
        "fields": ["ev", "exch_ts", "local_ts", "px", "qty"],
        "data_ul": 5,
        "depth_levels": DEPTH_LEVELS,
        "symbol": symbol,
        "date": date_str,
        "dedup_removed": stats["dedup_removed"],
        "original_bidask_rows": stats["original_bidask_rows"],
        "original_tick_rows": stats["original_tick_rows"],
        "data_fingerprint": fingerprint,
        "generator_script": "ch_l2_export.py",
        "generator_version": "v1",
        "rng_seed": None,
        "export_timestamp": datetime.now(timezone.utc).isoformat(),
        "parameters": {"price_scale": CLICKHOUSE_PRICE_SCALE},
        "regimes_covered": ["real_market"],
        "lineage": {
            "derived_from": f"ClickHouse hft.market_data WHERE symbol='{symbol}'",
            "parent": "hft.market_data",
        },
    }

    meta_path = Path(str(npz_path) + ".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    log.info("wrote_meta", path=str(meta_path))
    return meta_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_date(date_str: str) -> int:
    """Parse YYYY-MM-DD to integer YYYYMMDD."""
    parts = date_str.split("-")
    if len(parts) != 3:
        raise ValueError(f"Invalid date format (expected YYYY-MM-DD): {date_str}")
    return int(parts[0]) * 10000 + int(parts[1]) * 100 + int(parts[2])


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Export L2 depth data from ClickHouse → hftbacktest .npz"
    )
    parser.add_argument("--symbol", required=True, help="Symbol (e.g. TXFC6)")
    parser.add_argument(
        "--date", required=True, help="Trading date YYYY-MM-DD (e.g. 2026-03-06)"
    )
    parser.add_argument("--host", required=True, help="ClickHouse host")
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument("--port", type=int, default=8123, help="ClickHouse HTTP port")
    parser.add_argument("--user", default="default", help="ClickHouse user")
    parser.add_argument("--password", default="", help="ClickHouse password")
    parser.add_argument(
        "--max-memory",
        type=int,
        default=2_500_000_000,
        help="ClickHouse max_memory_usage",
    )
    args = parser.parse_args(argv)

    date_int = _parse_date(args.date)

    rows = _fetch_rows(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        symbol=args.symbol,
        date_int=date_int,
        max_memory=args.max_memory,
    )

    if not rows:
        log.warning("no_data", symbol=args.symbol, date=args.date)
        return 1

    events, stats = convert_rows_to_events(rows)

    if len(events) == 0:
        log.warning("no_events_generated", symbol=args.symbol, date=args.date)
        return 1

    out_dir = Path(args.out)
    npz_name = f"{args.symbol}_{args.date.replace('-', '')}.npz"
    npz_path = out_dir / npz_name

    write_npz(events, npz_path)
    write_meta_json(
        npz_path,
        symbol=args.symbol,
        date_str=args.date,
        stats=stats,
    )

    log.info(
        "export_complete",
        symbol=args.symbol,
        date=args.date,
        events=stats["total_events"],
        dedup_removed=stats["dedup_removed"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
