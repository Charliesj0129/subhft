"""Compatibility shim for archived parquet_to_research helpers.

This module preserves the small helper surface that unit tests and any
archived tooling still import, without restoring the full retired script set.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

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
except ImportError:  # pragma: no cover
    event_dtype = None
    DEPTH_EVENT = TRADE_EVENT = BUY_EVENT = SELL_EVENT = EXCH_EVENT = LOCAL_EVENT = 0
    _HFTBT_AVAILABLE = False

_RESEARCH_DTYPE = np.dtype(
    [
        ("bid_qty", "f8"),
        ("ask_qty", "f8"),
        ("bid_px", "f8"),
        ("ask_px", "f8"),
        ("mid_price", "f8"),
        ("spread_bps", "f8"),
        ("volume", "f8"),
        ("local_ts", "i8"),
    ]
)

_DEFAULT_SCALE = 10_000

_TYPE_CANDIDATES = ["type", "event_type", "msg_type", "record_type"]
_SYMBOL_CANDIDATES = ["symbol", "code", "ticker", "instrument"]
_EXCH_TS_CANDIDATES = ["exch_ts", "ts", "timestamp", "exchange_ts", "recv_time", "time"]
_BID_PX_CANDIDATES = ["bid_price", "bid_px", "best_bid", "bid", "bid1_price", "bid_price_1"]
_ASK_PX_CANDIDATES = ["ask_price", "ask_px", "best_ask", "ask", "ask1_price", "ask_price_1"]
_BID_QTY_CANDIDATES = ["bid_qty", "bid_volume", "bid_size", "bid_qty_1", "bid1_qty"]
_ASK_QTY_CANDIDATES = ["ask_qty", "ask_volume", "ask_size", "ask_qty_1", "ask1_qty"]
_PRICE_CANDIDATES = ["price", "last_price", "trade_price", "px"]
_VOLUME_CANDIDATES = ["volume", "qty", "trade_qty", "trade_vol", "size"]


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
                100.0 * (self.bid_ask_defect_dropped + self.tick_defect_dropped) / max(1, self.total_input),
                4,
            ),
        }


def _first_col(df_columns: list[str], candidates: list[str]) -> str | None:
    cols = set(df_columns)
    for candidate in candidates:
        if candidate in cols:
            return candidate
    return None


def _col_val(row: Any, col: str | None, default: float = 0.0) -> float:
    if col is None:
        return default
    value = row.get(col) if isinstance(row, dict) else getattr(row, col, None)
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _col_val_str(row: Any, col: str, default: str = "") -> str:
    value = row.get(col) if isinstance(row, dict) else getattr(row, col, None)
    if value is None:
        return default
    return str(value)


def _build_hbt_event(ev: int, exch_ts: int, local_ts: int, px: float, qty: float) -> tuple[Any, ...]:
    return (ev, int(exch_ts), int(local_ts), float(px), float(qty), 0, 0, 0.0)


def convert_symbol(
    df: Any,
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
    price_scale: int = _DEFAULT_SCALE,
    limit: int | None = None,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], DefectStats]:
    del symbol, col_symbol
    stats = DefectStats()
    hbt_events: list[tuple[Any, ...]] = []
    research_rows: list[tuple[Any, ...]] = []

    last_bid_px = 0.0
    last_ask_px = 0.0
    last_bid_qty = 1.0
    last_ask_qty = 1.0
    last_trade_px = 0.0

    rows = df.itertuples(index=False)
    if limit is not None:
        import itertools

        rows = itertools.islice(rows, limit)

    for row in rows:
        stats.total_input += 1
        etype = str(_col_val_str(row, col_type, "")).strip() if col_type is not None else "BidAsk"
        exch_ts = int(_col_val(row, col_exch_ts, 0))
        local_ts = exch_ts

        if etype == "Snapshot":
            stats.snapshots_skipped += 1
            continue

        if etype in ("BidAsk", "Quote"):
            bid_px_raw = _col_val(row, col_bid_px, 0.0)
            ask_px_raw = _col_val(row, col_ask_px, 0.0)
            bid_qty_raw = _col_val(row, col_bid_qty, 1.0)
            ask_qty_raw = _col_val(row, col_ask_qty, 1.0)

            bid_px = bid_px_raw / price_scale if bid_px_raw > price_scale else bid_px_raw
            ask_px = ask_px_raw / price_scale if ask_px_raw > price_scale else ask_px_raw
            bid_qty = bid_qty_raw if bid_qty_raw > 0 else 1.0
            ask_qty = ask_qty_raw if ask_qty_raw > 0 else 1.0

            if bid_px == 0.0 and ask_px == 0.0:
                stats.bid_ask_defect_dropped += 1
                continue
            if bid_px == 0.0 and ask_px > 0.0:
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

            if _HFTBT_AVAILABLE:
                ev_bid = DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT
                ev_ask = DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT
                hbt_events.append(_build_hbt_event(ev_bid, exch_ts, local_ts, bid_px, bid_qty))
                hbt_events.append(_build_hbt_event(ev_ask, exch_ts, local_ts, ask_px, ask_qty))

            mid = (bid_px + ask_px) / 2.0
            spread_bps = (ask_px - bid_px) / mid * 10_000.0 if mid > 0.0 else 0.0
            research_rows.append((bid_qty, ask_qty, bid_px, ask_px, mid, spread_bps, 0.0, local_ts))
            stats.output_rows += 1
            continue

        if etype in ("Tick", "Trade"):
            price_raw = _col_val(row, col_price, 0.0)
            volume = _col_val(row, col_volume, 0.0)
            price = price_raw / price_scale if price_raw > price_scale else price_raw

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

            mid = price
            bid_px = last_bid_px if last_bid_px > 0.0 else price
            ask_px = last_ask_px if last_ask_px > 0.0 else price
            spread_bps = (ask_px - bid_px) / mid * 10_000.0 if mid > 0.0 else 0.0
            research_rows.append((last_bid_qty, last_ask_qty, bid_px, ask_px, mid, spread_bps, volume, local_ts))
            stats.output_rows += 1

    return hbt_events, research_rows, stats


def _write_hftbt_npz(hbt_events: list[tuple[Any, ...]], out_path: Path) -> None:
    if not _HFTBT_AVAILABLE or not hbt_events:
        return
    arr = np.array(hbt_events, dtype=event_dtype)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(str(out_path), data=arr)


def _write_research_npy(research_rows: list[tuple[Any, ...]], out_path: Path) -> None:
    if not research_rows:
        return
    arr = np.array(research_rows, dtype=_RESEARCH_DTYPE)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(str(out_path), arr)


def _write_defect_report(stats: DefectStats, symbol: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"symbol": symbol}
    payload.update(stats.to_dict())
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_meta(
    research_rows: list[tuple[Any, ...]],
    symbol: str,
    input_path: str,
    out_path: Path,
    fingerprint: str,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "dataset_id": f"parquet_to_research_{symbol}_{uuid.uuid4().hex[:8]}",
        "source_type": "real",
        "source": str(input_path),
        "owner": "parquet_to_research.py",
        "schema_version": 1,
        "rows": len(research_rows),
        "fields": list(_RESEARCH_DTYPE.names),
        "symbols": [symbol],
        "rng_seed": None,
        "generator_script": "research/tools/parquet_to_research.py",
        "generator_version": "v1",
        "parameters": {"symbol": symbol, "source": str(input_path)},
        "regimes_covered": [],
        "data_fingerprint": fingerprint,
        "lineage": {"parent": str(input_path), "derived_from": "market_data_backup"},
        "data_ul": 3,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def detect_columns(df_columns: list[str]) -> dict[str, str | None]:
    cols: dict[str, str | None] = {}
    cols["type"] = _first_col(df_columns, _TYPE_CANDIDATES)
    cols["symbol"] = _first_col(df_columns, _SYMBOL_CANDIDATES)
    exch_ts = _first_col(df_columns, _EXCH_TS_CANDIDATES)
    if exch_ts is None:
        raise ValueError(f"Cannot find timestamp column in {df_columns}. Pass --col-exch-ts to specify it.")
    cols["exch_ts"] = exch_ts
    cols["bid_px"] = _first_col(df_columns, _BID_PX_CANDIDATES)
    cols["ask_px"] = _first_col(df_columns, _ASK_PX_CANDIDATES)
    cols["bid_qty"] = _first_col(df_columns, _BID_QTY_CANDIDATES)
    cols["ask_qty"] = _first_col(df_columns, _ASK_QTY_CANDIDATES)
    cols["price"] = _first_col(df_columns, _PRICE_CANDIDATES)
    cols["volume"] = _first_col(df_columns, _VOLUME_CANDIDATES)
    return cols
