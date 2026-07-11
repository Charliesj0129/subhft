"""Governed ClickHouse raw-data export helpers for research.

This module owns the canonical ``python -m research data-pipeline`` entrypoint
used by ``make research-export-l2-ticks``. It intentionally stays outside
runtime hot paths: all work is offline, read-only against ClickHouse, and writes
versioned raw research artifacts under ``research/data/raw``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from src.hft_platform.backtest.ch_data_source import (
    BUY_EVENT,
    DEPTH_CLEAR_EVENT,
    DEPTH_EVENT,
    EXCH_EVENT,
    LOCAL_EVENT,
    SELL_EVENT,
    TRADE_EVENT,
    _event_dtype,
    validate_events,
)

CH_PRICE_SCALE = 1_000_000.0
DEDUP_WINDOW_NS = 500_000
DEFAULT_OUT_DIR = Path("research/data/raw")

TICK_DTYPE = np.dtype(
    [
        ("exch_ts", "<i8"),
        ("local_ts", "<i8"),
        ("price", "<f8"),
        ("price_scaled", "<i8"),
        ("qty", "<f8"),
        ("side", "i1"),
    ]
)


def _dotenv_value(key: str, *, env_path: Path = Path(".env")) -> str:
    if os.environ.get(key):
        return str(os.environ[key])
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def _fingerprint(arr: np.ndarray) -> str:
    return hashlib.sha256(arr.tobytes()[:4096]).hexdigest()


def _write_meta(path: Path, payload: dict[str, Any]) -> Path:
    meta_path = Path(str(path) + ".meta.json")
    meta_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return meta_path


def _build_hbt_event(ev: int, exch_ts: int, local_ts: int, px: float, qty: float) -> tuple[Any, ...]:
    return (int(ev), int(exch_ts), int(local_ts), float(px), float(qty), 0, 0, 0.0)


def _valid_levels(
    prices: Sequence[int],
    volumes: Sequence[int],
    *,
    price_scale: float,
) -> dict[float, float]:
    out: dict[float, float] = {}
    for price, volume in zip(prices, volumes, strict=False):
        if int(price) > 0 and int(volume) > 0:
            out[float(price) / price_scale] = float(volume)
    return out


def _infer_trade_side(price: float, best_bid: float, best_ask: float, last_trade_price: float) -> int:
    if best_ask > 0.0 and price >= best_ask:
        return 1
    if best_bid > 0.0 and price <= best_bid:
        return -1
    if last_trade_price > 0.0 and price > last_trade_price:
        return 1
    if last_trade_price > 0.0 and price < last_trade_price:
        return -1
    return 1


def rows_to_l2_and_ticks(  # noqa: C901 - sequential protocol conversion mirrors event order.
    rows: Iterable[Sequence[Any]],
    *,
    price_scale: float = CH_PRICE_SCALE,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Convert ordered ClickHouse rows into hftbacktest L2 and tick arrays."""
    event_dtype = _event_dtype()
    events: list[tuple[Any, ...]] = []
    ticks: list[tuple[Any, ...]] = []
    snapshot_written = False
    prev_bidask_key: tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...], tuple[int, ...]] | None = None
    prev_bidask_ts = 0
    dedup_removed = 0
    best_bid = 0.0
    best_ask = 0.0
    last_trade_price = 0.0

    ev_bid_depth = int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT)
    ev_ask_depth = int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT)
    ev_clear = int(DEPTH_CLEAR_EVENT | EXCH_EVENT | LOCAL_EVENT)
    ev_trade_buy = int(TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT)
    ev_trade_sell = int(TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT)

    for row in rows:
        row_type, exch_ts, local_ts, bids_price, asks_price, bids_vol, asks_vol, px_raw, volume = row
        ts = int(exch_ts)
        local_ts = int(local_ts)
        if row_type == "BidAsk":
            bp_list = [int(v) for v in (bids_price or [])]
            ap_list = [int(v) for v in (asks_price or [])]
            bv_list = [int(v) for v in (bids_vol or [])]
            av_list = [int(v) for v in (asks_vol or [])]
            key = (tuple(bp_list), tuple(ap_list), tuple(bv_list), tuple(av_list))
            if key == prev_bidask_key and (ts - prev_bidask_ts) < DEDUP_WINDOW_NS:
                dedup_removed += 1
                continue
            prev_bidask_key = key
            prev_bidask_ts = ts

            bids = _valid_levels(bp_list, bv_list, price_scale=price_scale)
            asks = _valid_levels(ap_list, av_list, price_scale=price_scale)
            if not bids or not asks:
                continue

            events.append(_build_hbt_event(ev_clear, ts, local_ts, 0.0, 0.0))
            for price, qty in bids.items():
                events.append(_build_hbt_event(ev_bid_depth, ts, local_ts, price, qty))
            for price, qty in asks.items():
                events.append(_build_hbt_event(ev_ask_depth, ts, local_ts, price, qty))

            snapshot_written = True
            best_bid = max(bids)
            best_ask = min(asks)

        elif row_type == "Tick" and snapshot_written:
            px_int = int(px_raw or 0)
            qty = float(volume or 0)
            if px_int <= 0 or qty <= 0:
                continue
            price = float(px_int) / price_scale
            side = _infer_trade_side(price, best_bid, best_ask, last_trade_price)
            last_trade_price = price
            ev = ev_trade_buy if side > 0 else ev_trade_sell
            events.append(_build_hbt_event(ev, ts, local_ts, price, qty))
            ticks.append((ts, local_ts, price, px_int, qty, side))

    return np.array(events, dtype=event_dtype), np.array(ticks, dtype=TICK_DTYPE), dedup_removed


def _get_client(host: str, port: int, user: str, password: str) -> Any:
    import clickhouse_connect  # noqa: PLC0415

    return clickhouse_connect.get_client(host=host, port=port, username=user, password=password)


def _discover_symbol_dates(client: Any, symbol: str, date_from: str, date_to: str) -> list[tuple[str, int]]:
    query = """
        SELECT
            toDate(fromUnixTimestamp64Nano(ingest_ts), 'Asia/Taipei') AS dt,
            count() AS rows
        FROM hft.market_data
        WHERE symbol = %(symbol)s
          AND ingest_ts >= toUnixTimestamp64Nano(toDateTime64(%(date_from)s, 9, 'Asia/Taipei'))
          AND ingest_ts < toUnixTimestamp64Nano(toDateTime64(%(date_to_next)s, 9, 'Asia/Taipei'))
        GROUP BY dt
        HAVING rows > 0
        ORDER BY dt
    """
    from datetime import date, timedelta

    end = date.fromisoformat(date_to) + timedelta(days=1)
    result = client.query(
        query,
        parameters={
            "symbol": symbol,
            "date_from": f"{date_from} 00:00:00",
            "date_to_next": f"{end.isoformat()} 00:00:00",
        },
        settings={"max_memory_usage": 2_500_000_000, "max_threads": 2},
    )
    return [(str(row[0]), int(row[1])) for row in result.result_rows]


def _fetch_day_rows(client: Any, symbol: str, date: str) -> list[Sequence[Any]]:
    query = """
        SELECT
            type,
            exch_ts,
            ingest_ts AS local_ts,
            bids_price,
            asks_price,
            bids_vol,
            asks_vol,
            price_scaled,
            volume
        FROM hft.market_data
        WHERE symbol = %(symbol)s
          AND toDate(fromUnixTimestamp64Nano(ingest_ts), 'Asia/Taipei') = %(date)s
        ORDER BY exch_ts, ingest_ts, seq_no
    """
    result = client.query(
        query,
        parameters={"symbol": symbol, "date": date},
        settings={"max_memory_usage": 2_500_000_000, "max_threads": 2},
    )
    return list(result.result_rows)


def _write_day_outputs(
    *,
    symbol: str,
    date: str,
    out_dir: Path,
    events: np.ndarray,
    ticks: np.ndarray,
    dedup_removed: int,
    owner: str,
    overwrite: bool,
) -> dict[str, Any]:
    sym_dir = out_dir / symbol.lower()
    sym_dir.mkdir(parents=True, exist_ok=True)
    l2_path = sym_dir / f"{symbol}_{date}_l2.hftbt.npz"
    tick_path = sym_dir / f"{symbol}_{date}_ticks.npy"
    if not overwrite and (l2_path.exists() or tick_path.exists()):
        return {
            "symbol": symbol,
            "date": date,
            "status": "skipped_exists",
            "l2_path": str(l2_path),
            "tick_path": str(tick_path),
        }

    validate_events(events, symbol)
    np.savez_compressed(str(l2_path), data=events)
    np.save(str(tick_path), ticks)
    created_at = datetime.now(UTC).isoformat()

    l2_meta = {
        "created_at": created_at,
        "data_file": l2_path.name,
        "data_fingerprint": _fingerprint(events),
        "data_kind": "l2_hftbacktest",
        "data_ul": 5,
        "dataset_id": f"{symbol}_{date}_l2_hftbacktest",
        "date": date,
        "dedup_removed": int(dedup_removed),
        "depth_levels": 5,
        "fields": list(events.dtype.names or ()),
        "generator": "research.data_pipeline.export_l2_ticks",
        "owner": owner,
        "price_scale_applied": CH_PRICE_SCALE,
        "row_count": int(len(events)),
        "rows": int(len(events)),
        "schema_version": 1,
        "source": "hft.market_data",
        "source_type": "real",
        "split": "full",
        "symbols": [symbol],
    }
    tick_meta = {
        "created_at": created_at,
        "data_file": tick_path.name,
        "data_fingerprint": _fingerprint(ticks),
        "data_kind": "tick",
        "data_ul": 5,
        "dataset_id": f"{symbol}_{date}_tick",
        "date": date,
        "fields": list(ticks.dtype.names or ()),
        "generator": "research.data_pipeline.export_l2_ticks",
        "owner": owner,
        "price_scale_applied": CH_PRICE_SCALE,
        "row_count": int(len(ticks)),
        "rows": int(len(ticks)),
        "schema_version": 1,
        "source": "hft.market_data",
        "source_type": "real",
        "split": "full",
        "symbols": [symbol],
    }
    _write_meta(l2_path, l2_meta)
    _write_meta(tick_path, tick_meta)
    return {
        "symbol": symbol,
        "date": date,
        "status": "exported",
        "l2_path": str(l2_path),
        "tick_path": str(tick_path),
        "l2_rows": int(len(events)),
        "tick_rows": int(len(ticks)),
        "dedup_removed": int(dedup_removed),
    }


def export_l2_ticks(
    *,
    symbols: Sequence[str],
    date_from: str,
    date_to: str,
    host: str,
    port: int,
    user: str,
    password: str,
    out_dir: Path = DEFAULT_OUT_DIR,
    owner: str = "research",
    dry_run: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    client = _get_client(host, port, user, password)
    summary: dict[str, Any] = {
        "schema": "research.data_pipeline.export_l2_ticks.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "date_from": date_from,
        "date_to": date_to,
        "symbols": list(symbols),
        "out_dir": str(out_dir),
        "dry_run": dry_run,
        "outputs": [],
        "errors": [],
    }
    for symbol in symbols:
        for date, source_rows in _discover_symbol_dates(client, symbol, date_from, date_to):
            if dry_run:
                summary["outputs"].append(
                    {"symbol": symbol, "date": date, "status": "would_export", "source_rows": source_rows}
                )
                continue
            try:
                rows = _fetch_day_rows(client, symbol, date)
                events, ticks, dedup_removed = rows_to_l2_and_ticks(rows)
                if len(events) == 0 or len(ticks) == 0:
                    summary["errors"].append({"symbol": symbol, "date": date, "error": "empty_export"})
                    continue
                output = _write_day_outputs(
                    symbol=symbol,
                    date=date,
                    out_dir=out_dir,
                    events=events,
                    ticks=ticks,
                    dedup_removed=dedup_removed,
                    owner=owner,
                    overwrite=overwrite,
                )
                output["source_rows"] = source_rows
                summary["outputs"].append(output)
            except Exception as exc:  # noqa: BLE001 - CLI should collect per-day export failures.
                summary["errors"].append({"symbol": symbol, "date": date, "error": str(exc)})
    return summary


def validate_dataset(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    arr = np.load(path, allow_pickle=False)
    try:
        data = arr["data"] if isinstance(arr, np.lib.npyio.NpzFile) else arr
        if path.name.endswith("_l2.hftbt.npz"):
            validate_events(np.asarray(data), path.name)
        rows = int(len(data))
        fields = list(data.dtype.names or ())
    finally:
        if isinstance(arr, np.lib.npyio.NpzFile):
            arr.close()
    meta_path = Path(str(path) + ".meta.json")
    return {"ok": True, "path": str(path), "rows": rows, "fields": fields, "meta_exists": meta_path.exists()}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Governed research data pipeline.")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export-l2-ticks", help="Export ClickHouse L2 hftbacktest NPZ plus tick NPY")
    export.add_argument("--symbols", required=True, help="Comma-separated symbols")
    export.add_argument("--date-from", required=True)
    export.add_argument("--date-to", required=True)
    export.add_argument("--host", default=os.getenv("HFT_CLICKHOUSE_HOST", "localhost"))
    export.add_argument("--port", type=int, default=int(os.getenv("HFT_CLICKHOUSE_PORT", "8123")))
    export.add_argument("--user", default=os.getenv("HFT_CLICKHOUSE_USER", "default"))
    export.add_argument("--password", default=None)
    export.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    export.add_argument("--owner", default="research")
    export.add_argument("--dry-run", action="store_true")
    export.add_argument("--overwrite", action="store_true")

    validate = sub.add_parser("validate", help="Validate exported L2/tick dataset")
    validate.add_argument("--path", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "export-l2-ticks":
        password = args.password if args.password is not None else _dotenv_value("CLICKHOUSE_PASSWORD")
        summary = export_l2_ticks(
            symbols=[s.strip() for s in args.symbols.split(",") if s.strip()],
            date_from=args.date_from,
            date_to=args.date_to,
            host=args.host,
            port=args.port,
            user=args.user,
            password=password,
            out_dir=Path(args.out_dir),
            owner=args.owner,
            dry_run=bool(args.dry_run),
            overwrite=bool(args.overwrite),
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1 if summary["errors"] else 0
    if args.command == "validate":
        print(json.dumps(validate_dataset(Path(args.path)), indent=2, sort_keys=True))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
