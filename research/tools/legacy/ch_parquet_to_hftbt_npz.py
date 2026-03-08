#!/usr/bin/env python3
"""Convert ClickHouse-exported Parquet (L1/L2 LOB) → hftbacktest V2 AOS .npz.

Usage:
    uv run python research/tools/ch_parquet_to_hftbt_npz.py \
        research/data/remote_golden/golden_TXFC6.parquet \
        --symbol TXFC6 \
        --out research/data/remote_golden/golden_TXFC6.npz

Expected Parquet columns:
    exch_ts   (Int64, nanoseconds)
    ingest_ts (Int64, nanoseconds)
    bid_px    (Int64, scaled price)
    bid_qty   (Int64)
    ask_px    (Int64, scaled price)
    ask_qty   (Int64)
    volume    (Int64, optional)
    price_scaled (Int64, optional — trade price if volume > 0)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


def _build_event_dtype():
    """Return the hftbacktest event_dtype, importing from the library if available."""
    try:
        from hftbacktest.types import event_dtype
        return event_dtype
    except ImportError:
        # Fallback: define manually (must match hftbacktest V2 exactly)
        return np.dtype([
            ("ev", "<u8"),
            ("exch_ts", "<i8"),
            ("local_ts", "<i8"),
            ("px", "<f8"),
            ("qty", "<f8"),
        ])


def _event_flags():
    """Get hftbacktest event flag constants."""
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
        return {
            "DEPTH_SNAPSHOT_BID": int(DEPTH_SNAPSHOT_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT),
            "DEPTH_SNAPSHOT_ASK": int(DEPTH_SNAPSHOT_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT),
            "DEPTH_BID": int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT),
            "DEPTH_ASK": int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT),
            "TRADE": int(TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT),
        }
    except ImportError:
        # Fallback constants matching hftbacktest v2
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


def convert_parquet_to_hftbt_npz(
    parquet_path: str,
    out_path: str,
    *,
    price_scale: float = 1e9,  # ClickHouse prices are scaled by 1e9
) -> tuple[int, int]:
    """Convert a ClickHouse-exported Parquet file to hftbacktest V2 AOS .npz.

    Returns: (n_input_rows, n_output_events)
    """
    df = pd.read_parquet(parquet_path)
    n_rows = len(df)
    if n_rows == 0:
        raise ValueError(f"Empty Parquet file: {parquet_path}")

    # Sort by exch_ts to ensure chronological order
    df = df.sort_values("exch_ts").reset_index(drop=True)

    evt_dtype = _build_event_dtype()
    flags = _event_flags()

    # Pre-allocate: worst case = 1 snapshot pair + 2 depth + 1 trade per row
    max_events = 2 + n_rows * 3  # snapshot(2) + depth(2) + trade(1) per row
    events = np.zeros(max_events, dtype=evt_dtype)
    idx = 0

    # Extract arrays for speed
    exch_ts = df["exch_ts"].values.astype(np.int64)
    local_ts = df["ingest_ts"].values.astype(np.int64) if "ingest_ts" in df.columns else exch_ts.copy()
    bid_px = df["bid_px"].values.astype(np.float64) / price_scale
    bid_qty = df["bid_qty"].values.astype(np.float64)
    ask_px = df["ask_px"].values.astype(np.float64) / price_scale
    ask_qty = df["ask_qty"].values.astype(np.float64)
    has_volume = "volume" in df.columns
    volume = df["volume"].values.astype(np.float64) if has_volume else np.zeros(n_rows, dtype=np.float64)
    has_trade_px = "price_scaled" in df.columns
    trade_px = df["price_scaled"].values.astype(np.float64) / price_scale if has_trade_px else None

    # First row: DEPTH_SNAPSHOT_EVENT
    events[idx] = (flags["DEPTH_SNAPSHOT_BID"], exch_ts[0], local_ts[0], bid_px[0], bid_qty[0])
    idx += 1
    events[idx] = (flags["DEPTH_SNAPSHOT_ASK"], exch_ts[0], local_ts[0], ask_px[0], ask_qty[0])
    idx += 1

    # Remaining rows: DEPTH_EVENT + optional TRADE_EVENT
    for i in range(1, n_rows):
        ts_e = exch_ts[i]
        ts_l = local_ts[i]
        bp = bid_px[i]
        bq = bid_qty[i]
        ap = ask_px[i]
        aq = ask_qty[i]

        if bp > 0 or bq > 0:
            events[idx] = (flags["DEPTH_BID"], ts_e, ts_l, bp, bq)
            idx += 1
        if ap > 0 or aq > 0:
            events[idx] = (flags["DEPTH_ASK"], ts_e, ts_l, ap, aq)
            idx += 1
        if has_volume and volume[i] > 0:
            tp = trade_px[i] if (has_trade_px and trade_px[i] > 0) else (bp + ap) / 2.0
            events[idx] = (flags["TRADE"], ts_e, ts_l, tp, volume[i])
            idx += 1

    # Trim to actual size
    events = events[:idx]

    np.savez_compressed(out_path, data=events)
    return n_rows, idx


def generate_meta_json(
    npz_path: str,
    *,
    symbol: str,
    source_parquet: str,
    n_rows: int,
    n_events: int,
) -> str:
    """Generate a UL5-grade .meta.json sidecar for the .npz file."""
    npz_p = Path(npz_path)
    data_fingerprint = hashlib.sha256(npz_p.read_bytes()).hexdigest()

    meta = {
        "dataset_id": f"golden_{symbol}",
        "source_type": "real",
        "owner": "data-steward",
        "schema_version": 1,
        "rows": n_rows,
        "events": n_events,
        "fields": ["ev", "exch_ts", "local_ts", "px", "qty"],
        "source": "clickhouse_hft.market_data",
        "generator": "ch_parquet_to_hftbt_npz",
        "generator_script": "research/tools/ch_parquet_to_hftbt_npz.py",
        "generator_version": "v1",
        "seed": None,
        "rng_seed": None,
        "symbols": [symbol],
        "split": "full",
        "data_file": str(npz_p.resolve()),
        "data_fingerprint": data_fingerprint,
        "data_ul": 5,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "parameters": {
            "source_parquet": source_parquet,
            "price_scale": 1e9,
        },
        "regimes_covered": ["real_market"],
        "lineage": {
            "derived_from": f"ClickHouse hft.market_data WHERE symbol='{symbol}'",
            "parent": source_parquet,
        },
    }

    meta_path = str(npz_p) + ".meta.json"
    Path(meta_path).write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    return meta_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert ClickHouse Parquet → hftbacktest V2 AOS .npz")
    parser.add_argument("parquet", help="Input Parquet file path")
    parser.add_argument("--out", required=True, help="Output .npz file path")
    parser.add_argument("--symbol", required=True, help="Symbol name (e.g. TXFC6)")
    parser.add_argument("--price-scale", type=float, default=1e9, help="Price scale factor (default: 1e9)")
    args = parser.parse_args()

    print(f"[ch_parquet_to_hftbt_npz] Converting {args.parquet} → {args.out}")
    n_rows, n_events = convert_parquet_to_hftbt_npz(
        args.parquet, args.out, price_scale=args.price_scale,
    )
    print(f"[ch_parquet_to_hftbt_npz] {n_rows:,} input rows → {n_events:,} events")

    meta_path = generate_meta_json(
        args.out,
        symbol=args.symbol,
        source_parquet=args.parquet,
        n_rows=n_rows,
        n_events=n_events,
    )
    print(f"[ch_parquet_to_hftbt_npz] Metadata: {meta_path}")

    # Quick validation
    from hft_platform.alpha.validation import _check_hftbacktest_v2_data_format
    errors = _check_hftbacktest_v2_data_format(args.out)
    if errors:
        print(f"[ch_parquet_to_hftbt_npz] ⚠️ V2 format warnings: {errors}")
        return 1
    else:
        print("[ch_parquet_to_hftbt_npz] ✅ V2 format validation passed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
