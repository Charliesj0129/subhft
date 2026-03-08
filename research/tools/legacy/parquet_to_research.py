"""parquet_to_research.py — Convert market_data_backup.parquet to research formats.

Part A of the dirty-data-repair + golden-data pipeline plan.

Outputs per symbol:
    research/data/processed/{symbol}/
    ├── hftbt.npz          # hftbacktest event_dtype NPZ (primary format)
    ├── research.npy       # ResearchBacktestRunner structured array (fallback)
    ├── defect_report.json # defect statistics
    └── meta.json          # UL3 sidecar

Defect repair rules:
    BidAsk, bid=0 AND ask=0     → discard (defect)
    BidAsk, bid=0, ask>0        → forward-fill last bid (bid_recovered)
    BidAsk, ask=0, bid>0        → forward-fill last ask (ask_recovered)
    Tick, price=0, volume>0     → forward-fill last trade price (price_ffill)
    Tick, price=0, volume=0     → discard (defect)
    Snapshot                    → skip (used for initial state only)
    ingest_ts (all)             → replace with exch_ts as local_ts
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Optional hftbacktest import (graceful degradation for hftbt.npz output)
# ---------------------------------------------------------------------------
try:
    from hftbacktest.types import (
        BUY_EVENT,
        DEPTH_EVENT,
        EXCH_EVENT,
        LOCAL_EVENT,
        SELL_EVENT,
        TRADE_EVENT,
        event_dtype,
    )
    _HFTBT_AVAILABLE = True
except ImportError:
    event_dtype = None
    DEPTH_EVENT = TRADE_EVENT = BUY_EVENT = SELL_EVENT = EXCH_EVENT = LOCAL_EVENT = 0
    _HFTBT_AVAILABLE = False

# research.npy dtype — mirrors synth_lob_gen._DTYPE
_RESEARCH_DTYPE = np.dtype([
    ("bid_qty", "f8"),
    ("ask_qty", "f8"),
    ("bid_px", "f8"),
    ("ask_px", "f8"),
    ("mid_price", "f8"),
    ("spread_bps", "f8"),
    ("volume", "f8"),
    ("local_ts", "i8"),
])

# Price scale used in the platform (prices stored as int x10000)
_DEFAULT_SCALE = 10_000


# ---------------------------------------------------------------------------
# Defect counters
# ---------------------------------------------------------------------------
@dataclass
class DefectStats:
    total_input: int = 0
    snapshots_skipped: int = 0
    bid_ask_defect_dropped: int = 0
    bid_recovered: int = 0
    ask_recovered: int = 0
    tick_price_ffill: int = 0
    tick_defect_dropped: int = 0
    output_rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_input": self.total_input,
            "snapshots_skipped": self.snapshots_skipped,
            "bid_ask_defect_dropped": self.bid_ask_defect_dropped,
            "bid_recovered": self.bid_recovered,
            "ask_recovered": self.ask_recovered,
            "tick_price_ffill": self.tick_price_ffill,
            "tick_defect_dropped": self.tick_defect_dropped,
            "output_rows": self.output_rows,
            "defect_rate_pct": round(
                100.0 * (
                    self.bid_ask_defect_dropped + self.tick_defect_dropped
                ) / max(1, self.total_input),
                4,
            ),
        }


# ---------------------------------------------------------------------------
# Column name resolution helpers
# ---------------------------------------------------------------------------
_TYPE_CANDIDATES = ["type", "event_type", "msg_type", "record_type"]
_SYMBOL_CANDIDATES = ["symbol", "code", "ticker", "instrument"]
_EXCH_TS_CANDIDATES = ["exch_ts", "ts", "timestamp", "exchange_ts", "recv_time", "time"]
_INGEST_TS_CANDIDATES = ["ingest_ts", "local_ts", "recv_ts", "system_ts"]
_BID_PX_CANDIDATES = ["bid_price", "bid_px", "best_bid", "bid", "bid1_price", "bid_price_1"]
_ASK_PX_CANDIDATES = ["ask_price", "ask_px", "best_ask", "ask", "ask1_price", "ask_price_1"]
_BID_QTY_CANDIDATES = ["bid_qty", "bid_volume", "bid_size", "bid_qty_1", "bid1_qty"]
_ASK_QTY_CANDIDATES = ["ask_qty", "ask_volume", "ask_size", "ask_qty_1", "ask1_qty"]
_PRICE_CANDIDATES = ["price", "last_price", "trade_price", "px"]
_VOLUME_CANDIDATES = ["volume", "qty", "trade_qty", "trade_vol", "size"]

# Additional L2/L5 bid/ask levels (bid_price_2 … bid_price_5)
_BID_LEVELS = [
    (["bid_price_%d" % i, "bid%d_price" % i, "bid_px_%d" % i], ["bid_qty_%d" % i, "bid%d_qty" % i, "bid_volume_%d" % i])
    for i in range(1, 6)
]
_ASK_LEVELS = [
    (["ask_price_%d" % i, "ask%d_price" % i, "ask_px_%d" % i], ["ask_qty_%d" % i, "ask%d_qty" % i, "ask_volume_%d" % i])
    for i in range(1, 6)
]


def _first_col(df_columns: list[str], candidates: list[str]) -> str | None:
    cols = set(df_columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def _col_val(row: Any, col: str | None, default: float = 0.0) -> float:
    if col is None:
        return default
    # pandas itertuples() rows are namedtuples — always use getattr.
    if isinstance(row, dict):
        v = row.get(col)
    else:
        v = getattr(row, col, None)
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Core per-symbol conversion
# ---------------------------------------------------------------------------
def _build_hbt_event(ev: int, exch_ts: int, local_ts: int, px: float, qty: float) -> tuple:
    """Build a single event tuple for event_dtype array."""
    return (ev, int(exch_ts), int(local_ts), float(px), float(qty), 0, 0, 0.0)


def convert_symbol(
    df: Any,  # pandas DataFrame filtered to one symbol
    symbol: str,
    col_type: str | None,
    col_symbol: str | None,
    col_exch_ts: str,
    col_bid_px: str | None,
    col_ask_px: str | None,
    col_bid_qty: str | None,
    col_ask_qty: str | None,
    col_price: str | None,
    col_volume: str | None,
    price_scale: int,
    limit: int | None,
) -> tuple[list[tuple], list[tuple], DefectStats]:
    """Convert rows for one symbol.

    Returns:
        hbt_events: list of tuples for event_dtype
        research_rows: list of tuples matching _RESEARCH_DTYPE
        stats: DefectStats
    """
    stats = DefectStats()
    hbt_events: list[tuple] = []
    research_rows: list[tuple] = []

    last_bid_px: float = 0.0
    last_ask_px: float = 0.0
    last_bid_qty: float = 1.0
    last_ask_qty: float = 1.0
    last_trade_px: float = 0.0

    rows = df.itertuples(index=False)
    if limit is not None:
        import itertools
        rows = itertools.islice(rows, limit)

    for row in rows:
        stats.total_input += 1

        # Determine event type
        if col_type is not None:
            etype = str(_col_val_str(row, col_type, "")).strip()
        else:
            etype = "BidAsk"  # assume BidAsk if no type column

        exch_ts_raw = _col_val(row, col_exch_ts, 0)
        exch_ts = int(exch_ts_raw)
        local_ts = exch_ts  # ingest_ts is invalid; use exch_ts

        if etype == "Snapshot":
            stats.snapshots_skipped += 1
            continue

        if etype in ("BidAsk", "Quote"):
            bid_px_raw = _col_val(row, col_bid_px, 0.0)
            ask_px_raw = _col_val(row, col_ask_px, 0.0)
            bid_qty_raw = _col_val(row, col_bid_qty, 1.0)
            ask_qty_raw = _col_val(row, col_ask_qty, 1.0)

            # Descale if stored as scaled integers
            bid_px = bid_px_raw / price_scale if bid_px_raw > price_scale else bid_px_raw
            ask_px = ask_px_raw / price_scale if ask_px_raw > price_scale else ask_px_raw
            bid_qty = bid_qty_raw if bid_qty_raw > 0 else 1.0
            ask_qty = ask_qty_raw if ask_qty_raw > 0 else 1.0

            # Repair rules
            if bid_px == 0.0 and ask_px == 0.0:
                stats.bid_ask_defect_dropped += 1
                continue
            elif bid_px == 0.0 and ask_px > 0.0:
                stats.bid_recovered += 1
                bid_px = last_bid_px if last_bid_px > 0.0 else ask_px * 0.9999
            elif ask_px == 0.0 and bid_px > 0.0:
                stats.ask_recovered += 1
                ask_px = last_ask_px if last_ask_px > 0.0 else bid_px * 1.0001

            last_bid_px = bid_px
            last_ask_px = ask_px
            last_bid_qty = bid_qty
            last_ask_qty = ask_qty

            if last_trade_px == 0.0:
                last_trade_px = (bid_px + ask_px) / 2.0

            # hftbt events: bid depth + ask depth
            if _HFTBT_AVAILABLE:
                ev_bid = DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT
                ev_ask = DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT
                hbt_events.append(_build_hbt_event(ev_bid, exch_ts, local_ts, bid_px, bid_qty))
                hbt_events.append(_build_hbt_event(ev_ask, exch_ts, local_ts, ask_px, ask_qty))

            # research row
            mid = (bid_px + ask_px) / 2.0
            spread_bps = (ask_px - bid_px) / mid * 10_000.0 if mid > 0.0 else 0.0
            research_rows.append((bid_qty, ask_qty, bid_px, ask_px, mid, spread_bps, 0.0, local_ts))
            stats.output_rows += 1

        elif etype in ("Tick", "Trade"):
            price_raw = _col_val(row, col_price, 0.0)
            vol_raw = _col_val(row, col_volume, 0.0)

            price = price_raw / price_scale if price_raw > price_scale else price_raw
            volume = vol_raw

            if price == 0.0 and volume > 0.0:
                stats.tick_price_ffill += 1
                price = last_trade_px
            elif price == 0.0 and volume == 0.0:
                stats.tick_defect_dropped += 1
                continue

            last_trade_px = price

            if _HFTBT_AVAILABLE:
                ev_trade = TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT
                hbt_events.append(_build_hbt_event(ev_trade, exch_ts, local_ts, price, volume))

            # research row: use last L1 for bid/ask context
            mid = price
            bid_px = last_bid_px if last_bid_px > 0 else price
            ask_px = last_ask_px if last_ask_px > 0 else price
            spread_bps = (ask_px - bid_px) / mid * 10_000.0 if mid > 0.0 else 0.0
            research_rows.append((
                last_bid_qty, last_ask_qty, bid_px, ask_px, mid, spread_bps, volume, local_ts
            ))
            stats.output_rows += 1

    return hbt_events, research_rows, stats


def _col_val_str(row: Any, col: str, default: str = "") -> str:
    if isinstance(row, dict):
        v = row.get(col)
    else:
        v = getattr(row, col, None)
    if v is None:
        return default
    return str(v)


# ---------------------------------------------------------------------------
# File writers
# ---------------------------------------------------------------------------
def _write_hftbt_npz(hbt_events: list[tuple], out_path: Path) -> None:
    if not _HFTBT_AVAILABLE:
        return
    if not hbt_events:
        return
    arr = np.array(hbt_events, dtype=event_dtype)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(str(out_path), data=arr)


def _write_research_npy(research_rows: list[tuple], out_path: Path) -> None:
    if not research_rows:
        return
    arr = np.array(research_rows, dtype=_RESEARCH_DTYPE)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(str(out_path), arr)


def _write_defect_report(stats: DefectStats, symbol: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = {"symbol": symbol}
    data.update(stats.to_dict())
    out_path.write_text(json.dumps(data, indent=2))


def _write_meta(
    research_rows: list[tuple],
    symbol: str,
    input_path: str,
    out_path: Path,
    fingerprint: str,
) -> None:
    """Write UL3-compliant metadata sidecar."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "dataset_id": f"parquet_to_research_{symbol}_{uuid.uuid4().hex[:8]}",
        "source_type": "real",
        "source": str(input_path),
        "owner": "parquet_to_research.py",
        "schema_version": 1,
        "rows": len(research_rows),
        "fields": list(_RESEARCH_DTYPE.names),
        "symbols": [symbol],
        # UL3 fields
        "rng_seed": None,
        "generator_script": "research/tools/parquet_to_research.py",
        "generator_version": "v1",
        "parameters": {
            "symbol": symbol,
            "source": str(input_path),
        },
        # UL4 fields
        "regimes_covered": [],
        # UL5 fields
        "data_fingerprint": fingerprint,
        "lineage": {"parent": str(input_path), "derived_from": "market_data_backup"},
        "data_ul": 3,
    }
    out_path.write_text(json.dumps(meta, indent=2))


# ---------------------------------------------------------------------------
# Top-level per-symbol pipeline
# ---------------------------------------------------------------------------
def process_symbol(
    df: Any,
    symbol: str,
    columns: dict[str, str | None],
    out_dir: Path,
    price_scale: int,
    limit: int | None,
) -> DefectStats:
    hbt_events, research_rows, stats = convert_symbol(
        df=df,
        symbol=symbol,
        col_type=columns.get("type"),
        col_symbol=columns.get("symbol"),
        col_exch_ts=columns["exch_ts"],
        col_bid_px=columns.get("bid_px"),
        col_ask_px=columns.get("ask_px"),
        col_bid_qty=columns.get("bid_qty"),
        col_ask_qty=columns.get("ask_qty"),
        col_price=columns.get("price"),
        col_volume=columns.get("volume"),
        price_scale=price_scale,
        limit=limit,
    )

    sym_dir = out_dir / symbol
    sym_dir.mkdir(parents=True, exist_ok=True)

    # Compute fingerprint of research rows
    if research_rows:
        arr_preview = np.array(research_rows[:min(128, len(research_rows))], dtype=_RESEARCH_DTYPE)
        fingerprint = hashlib.sha256(arr_preview.tobytes()).hexdigest()[:16]
    else:
        fingerprint = "empty"

    if _HFTBT_AVAILABLE and hbt_events:
        _write_hftbt_npz(hbt_events, sym_dir / "hftbt.npz")
        print(f"  [hftbt.npz] {len(hbt_events)} events")

    if research_rows:
        _write_research_npy(research_rows, sym_dir / "research.npy")
        print(f"  [research.npy] {len(research_rows)} rows")

    _write_defect_report(stats, symbol, sym_dir / "defect_report.json")
    _write_meta(research_rows, symbol, "market_data_backup.parquet", sym_dir / "meta.json", fingerprint)

    print(f"  [defects] dropped={stats.bid_ask_defect_dropped + stats.tick_defect_dropped}"
          f"  bid_recovered={stats.bid_recovered}  ask_recovered={stats.ask_recovered}"
          f"  tick_ffill={stats.tick_price_ffill}")

    return stats


# ---------------------------------------------------------------------------
# Column auto-detection
# ---------------------------------------------------------------------------
def detect_columns(df_columns: list[str]) -> dict[str, str | None]:
    cols: dict[str, str | None] = {}
    cols["type"] = _first_col(df_columns, _TYPE_CANDIDATES)
    cols["symbol"] = _first_col(df_columns, _SYMBOL_CANDIDATES)
    exch_ts = _first_col(df_columns, _EXCH_TS_CANDIDATES)
    if exch_ts is None:
        raise ValueError(f"Cannot find timestamp column in {df_columns}. "
                         "Pass --col-exch-ts to specify it.")
    cols["exch_ts"] = exch_ts
    cols["bid_px"] = _first_col(df_columns, _BID_PX_CANDIDATES)
    cols["ask_px"] = _first_col(df_columns, _ASK_PX_CANDIDATES)
    cols["bid_qty"] = _first_col(df_columns, _BID_QTY_CANDIDATES)
    cols["ask_qty"] = _first_col(df_columns, _ASK_QTY_CANDIDATES)
    cols["price"] = _first_col(df_columns, _PRICE_CANDIDATES)
    cols["volume"] = _first_col(df_columns, _VOLUME_CANDIDATES)
    return cols


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Convert market_data_backup.parquet to hftbacktest + research formats."
    )
    p.add_argument("--input", required=True, help="Path to input parquet file")
    p.add_argument("--symbols", required=False, default="",
                   help="Comma-separated symbol list (empty = all symbols)")
    p.add_argument("--out-dir", default="research/data/processed",
                   help="Output root directory (default: research/data/processed)")
    p.add_argument("--price-scale", type=int, default=_DEFAULT_SCALE,
                   help=f"Price scale divisor (default: {_DEFAULT_SCALE})")
    p.add_argument("--limit", type=int, default=None,
                   help="Limit rows per symbol (for testing)")
    p.add_argument("--col-type", default=None, help="Override 'type' column name")
    p.add_argument("--col-symbol", default=None, help="Override 'symbol' column name")
    p.add_argument("--col-exch-ts", default=None, help="Override exch_ts column name")
    p.add_argument("--col-bid-px", default=None, help="Override bid_px column name")
    p.add_argument("--col-ask-px", default=None, help="Override ask_px column name")
    p.add_argument("--col-bid-qty", default=None, help="Override bid_qty column name")
    p.add_argument("--col-ask-qty", default=None, help="Override ask_qty column name")
    p.add_argument("--col-price", default=None, help="Override price column name")
    p.add_argument("--col-volume", default=None, help="Override volume column name")
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    try:
        import pandas as pd
    except ImportError:
        print("ERROR: pandas is required. Install with: pip install pandas pyarrow")
        return 1

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}")
        return 1

    out_dir = Path(args.out_dir)
    print(f"Reading parquet: {input_path} ...")
    df = pd.read_parquet(str(input_path))
    print(f"Loaded {len(df):,} rows, columns: {list(df.columns)}")

    # Detect columns
    col_overrides: dict[str, str | None] = {
        "type": args.col_type,
        "symbol": args.col_symbol,
        "exch_ts": args.col_exch_ts,
        "bid_px": args.col_bid_px,
        "ask_px": args.col_ask_px,
        "bid_qty": args.col_bid_qty,
        "ask_qty": args.col_ask_qty,
        "price": args.col_price,
        "volume": args.col_volume,
    }
    detected = detect_columns(list(df.columns))
    columns = {k: (col_overrides.get(k) or detected.get(k)) for k in detected}
    print(f"Column mapping: {columns}")

    # Determine symbols
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        sym_col = columns.get("symbol")
        if sym_col and sym_col in df.columns:
            symbols = sorted(df[sym_col].dropna().unique().tolist())
        else:
            symbols = ["ALL"]

    print(f"Processing symbols: {symbols}")
    if not _HFTBT_AVAILABLE:
        print("WARNING: hftbacktest not installed — hftbt.npz will be skipped.")

    total_stats = DefectStats()
    for symbol in symbols:
        print(f"\n--- {symbol} ---")
        sym_col = columns.get("symbol")
        if sym_col and sym_col in df.columns and symbol != "ALL":
            sym_df = df[df[sym_col] == symbol].copy()
        else:
            sym_df = df.copy()

        if len(sym_df) == 0:
            print(f"  No rows for {symbol}, skipping.")
            continue

        # Sort by exchange timestamp
        exch_ts_col = columns["exch_ts"]
        if exch_ts_col and exch_ts_col in sym_df.columns:
            sym_df = sym_df.sort_values(exch_ts_col)

        stats = process_symbol(
            df=sym_df,
            symbol=symbol,
            columns=columns,
            out_dir=out_dir,
            price_scale=args.price_scale,
            limit=args.limit,
        )
        total_stats.total_input += stats.total_input
        total_stats.snapshots_skipped += stats.snapshots_skipped
        total_stats.bid_ask_defect_dropped += stats.bid_ask_defect_dropped
        total_stats.tick_defect_dropped += stats.tick_defect_dropped
        total_stats.bid_recovered += stats.bid_recovered
        total_stats.ask_recovered += stats.ask_recovered
        total_stats.tick_price_ffill += stats.tick_price_ffill
        total_stats.output_rows += stats.output_rows

    print(f"\n=== Summary ===")
    print(json.dumps(total_stats.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
