from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

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
    from hftbacktest.types import event_dtype as _HBT_EVENT_DTYPE
except ImportError:  # pragma: no cover
    _HBT_EVENT_DTYPE = np.dtype(
        [
            ("ev", "<u8"),
            ("exch_ts", "<i8"),
            ("local_ts", "<i8"),
            ("px", "<f8"),
            ("qty", "<f8"),
            ("order_id", "<u8"),
            ("ival", "<i8"),
            ("fval", "<f8"),
        ]
    )
    EXCH_EVENT = 1 << 31
    LOCAL_EVENT = 1 << 30
    BUY_EVENT = 1 << 29
    SELL_EVENT = 1 << 28
    DEPTH_EVENT = 1 << 21
    TRADE_EVENT = 1 << 20
    DEPTH_SNAPSHOT_EVENT = 1 << 19


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

_BUY_DEPTH_EVENT = int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT)
_SELL_DEPTH_EVENT = int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT)
_BUY_SNAPSHOT_EVENT = int(DEPTH_SNAPSHOT_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT)
_SELL_SNAPSHOT_EVENT = int(DEPTH_SNAPSHOT_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT)
_TRADE_EVENT_CODE = int(TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT)
_DATE_TOKEN_RE = re.compile(r"(?P<date>\d{8}|\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True)
class GovernedBundle:
    primary_data: Path
    hftbt_path: Path
    hftbt_snapshot_path: Path
    primary_meta: Path
    hftbt_meta: Path
    hftbt_snapshot_meta: Path
    bundle_audit: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "research_path": str(self.primary_data),
            "hftbt_path": str(self.hftbt_path),
            "hftbt_snapshot_path": str(self.hftbt_snapshot_path),
            "research_meta_path": str(self.primary_meta),
            "hftbt_meta_path": str(self.hftbt_meta),
            "hftbt_snapshot_meta_path": str(self.hftbt_snapshot_meta),
            "bundle_audit_path": str(self.bundle_audit),
        }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _meta_path_for(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".meta.json")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_event(ev: int, exch_ts: int, local_ts: int, px: float, qty: float) -> tuple[Any, ...]:
    return (int(ev), int(exch_ts), int(local_ts), float(px), float(qty), 0, 0, 0.0)


def _spread_bps(bid_px: float, ask_px: float) -> float:
    mid = (bid_px + ask_px) / 2.0
    if bid_px <= 0.0 or ask_px <= 0.0 or mid <= 0.0:
        return 0.0
    return (ask_px - bid_px) / mid * 10_000.0


def _first_scalar(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        if value.size == 0:
            return None
        return value.reshape(-1)[0].item()
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _price_to_float(value: Any, *, price_scale: float) -> float:
    if value is None:
        return 0.0
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return 0.0
    if raw == 0.0:
        return 0.0
    if abs(raw) >= price_scale:
        return raw / price_scale
    return raw


def _infer_symbol_from_path(path: Path) -> str | None:
    stem = path.stem
    date_match = _DATE_TOKEN_RE.search(stem)
    token = stem[: date_match.start()] if date_match else stem
    token = token.rstrip("_-")
    return token.upper() or None


def _infer_split_tag(path: Path, source_name: str) -> str:
    date_match = _DATE_TOKEN_RE.search(path.stem)
    if date_match:
        return f"{source_name}_{date_match.group('date').replace('-', '')}"
    return source_name


def _iter_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                records.append(payload)
    return records


def _build_meta(
    *,
    path: Path,
    rows: int,
    fields: tuple[str, ...],
    fingerprint: str,
    owner: str,
    source_name: str,
    source_type: str,
    split: str,
    symbols: list[str],
    parameters: dict[str, Any],
) -> dict[str, Any]:
    return {
        "created_at": _now_iso(),
        "data_file": str(path),
        "data_fingerprint": fingerprint,
        "data_ul": 5,
        "fields": list(fields),
        "generator": "prepare_governed_data",
        "generator_script": "research/tools/prepare_governed_data.py",
        "generator_version": "v1",
        "owner": owner,
        "parameters": parameters,
        "regimes_covered": ["real_market"] if source_type == "real" else ["synthetic"],
        "rows": int(rows),
        "schema_version": 1,
        "source": source_name,
        "source_type": source_type,
        "split": split,
        "symbols": symbols,
    }


def audit_governed_bundle(path: str | Path) -> dict[str, Any]:
    target = Path(path).resolve()
    bundle_dir = target if target.is_dir() else target.parent
    primary_candidates = sorted(bundle_dir.glob("*_research.npz")) + sorted(bundle_dir.glob("research.npy"))
    primary = target if target.is_file() else (primary_candidates[0] if primary_candidates else None)
    hftbt_path = bundle_dir / "hftbt.npz"
    snapshot_path = bundle_dir / "hftbt_snapshot.npz"

    warnings: list[str] = []
    errors: list[str] = []
    if primary is None or not primary.exists():
        errors.append("missing_primary_data")
    if not hftbt_path.exists():
        warnings.append("missing_hftbt")
    if not snapshot_path.exists():
        warnings.append("missing_hftbt_snapshot")
    if primary is not None and not _meta_path_for(primary).exists():
        warnings.append("missing_primary_meta")
    if hftbt_path.exists() and not _meta_path_for(hftbt_path).exists():
        warnings.append("missing_hftbt_meta")
    if snapshot_path.exists() and not _meta_path_for(snapshot_path).exists():
        warnings.append("missing_hftbt_snapshot_meta")

    payload = {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "bundle": {
            "dir": str(bundle_dir),
            "primary_data": str(primary) if primary else None,
            "backtest_data": str(hftbt_path),
            "backtest_snapshot": str(snapshot_path),
            "layout": "research_hftbt_bundle_v1",
        },
    }
    _write_json(bundle_dir / "bundle_audit.json", payload)
    return payload


def prepare_clickhouse_export(
    *,
    input_path: str | Path,
    output_dir: str | Path,
    alpha_id: str,
    owner: str = "research",
    split: str = "full",
    symbol: str | None = None,
    tag: str | None = None,
    source: str = "clickhouse_export",
    source_type: str = "real",
    price_scale: float = 1_000_000.0,
    limit: int | None = None,
    paper_refs: list[str] | None = None,
    chunk_size: int = 50_000,
) -> GovernedBundle:
    del chunk_size
    src = Path(input_path).resolve()
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    records = _iter_jsonl_records(src)
    if not records:
        raise ValueError("no valid rows found in input")

    resolved_symbol = symbol.upper() if symbol else _infer_symbol_from_path(src)
    if resolved_symbol is None:
        raise ValueError("unable to infer symbol from input")

    research_rows: list[tuple[Any, ...]] = []
    hftbt_rows: list[tuple[Any, ...]] = []
    snapshot_rows: list[tuple[Any, ...]] = []
    deferred_trades: list[tuple[int, float, float]] = []

    last_bid_px = 0.0
    last_ask_px = 0.0
    last_bid_qty = 0.0
    last_ask_qty = 0.0
    snapshot_written = False
    rows_seen = 0

    for record in records:
        rows_seen += 1
        if limit is not None and rows_seen > int(limit):
            break
        if str(record.get("symbol", resolved_symbol)).upper() != resolved_symbol:
            continue

        row_type = str(record.get("type", ""))
        ts = int(record.get("ingest_ts", record.get("local_ts", 0)) or 0)

        if row_type in {"BidAsk", "Quote"}:
            bid_px = _price_to_float(
                _first_scalar(record.get("bids_price", record.get("bid_price", record.get("bid_px")))),
                price_scale=price_scale,
            )
            ask_px = _price_to_float(
                _first_scalar(record.get("asks_price", record.get("ask_price", record.get("ask_px")))),
                price_scale=price_scale,
            )
            bid_qty = float(
                _first_scalar(record.get("bids_vol", record.get("bid_volume", record.get("bid_qty", 0.0))))
                or 0.0
            )
            ask_qty = float(
                _first_scalar(record.get("asks_vol", record.get("ask_volume", record.get("ask_qty", 0.0))))
                or 0.0
            )

            if bid_px <= 0.0 and ask_px <= 0.0:
                continue
            if bid_px <= 0.0:
                bid_px = last_bid_px if last_bid_px > 0.0 else ask_px
            if ask_px <= 0.0:
                ask_px = last_ask_px if last_ask_px > 0.0 else bid_px

            last_bid_px = bid_px
            last_ask_px = ask_px
            last_bid_qty = bid_qty
            last_ask_qty = ask_qty
            mid_price = (bid_px + ask_px) / 2.0
            research_rows.append((bid_qty, ask_qty, bid_px, ask_px, mid_price, _spread_bps(bid_px, ask_px), 0.0, ts))

            if not snapshot_written:
                snapshot_rows.append(_build_event(_BUY_SNAPSHOT_EVENT, ts, ts, bid_px, bid_qty))
                snapshot_rows.append(_build_event(_SELL_SNAPSHOT_EVENT, ts, ts, ask_px, ask_qty))
                hftbt_rows.extend(snapshot_rows)
                snapshot_written = True

            hftbt_rows.append(_build_event(_BUY_DEPTH_EVENT, ts, ts, bid_px, bid_qty))
            hftbt_rows.append(_build_event(_SELL_DEPTH_EVENT, ts, ts, ask_px, ask_qty))
            for trade_ts, trade_px, trade_qty in deferred_trades:
                hftbt_rows.append(_build_event(_TRADE_EVENT_CODE, trade_ts, trade_ts, trade_px, trade_qty))
            deferred_trades.clear()
            continue

        if row_type in {"Tick", "Trade"}:
            trade_px = _price_to_float(
                record.get("price_scaled", record.get("price", record.get("trade_price", record.get("px")))),
                price_scale=price_scale,
            )
            trade_qty = float(record.get("volume", record.get("qty", 0.0)) or 0.0)
            if trade_px <= 0.0 and trade_qty <= 0.0:
                continue

            bid_px = last_bid_px if last_bid_px > 0.0 else trade_px
            ask_px = last_ask_px if last_ask_px > 0.0 else trade_px
            bid_qty = last_bid_qty if last_bid_px > 0.0 else 0.0
            ask_qty = last_ask_qty if last_ask_px > 0.0 else 0.0
            research_rows.append(
                (bid_qty, ask_qty, bid_px, ask_px, trade_px, _spread_bps(bid_px, ask_px), trade_qty, ts)
            )

            if snapshot_written:
                hftbt_rows.append(_build_event(_TRADE_EVENT_CODE, ts, ts, trade_px, trade_qty))
            else:
                deferred_trades.append((ts, trade_px, trade_qty))

    if not research_rows:
        raise ValueError("no valid rows found in input")
    if not snapshot_rows:
        raise ValueError("no book snapshot could be built from input")

    if tag:
        base_name = f"{alpha_id}_{tag}_research"
    elif split != "full":
        base_name = f"{alpha_id}_{split}_research"
    else:
        base_name = f"{alpha_id}_{_infer_split_tag(src, source)}_research"
    research_path = out_dir / f"{base_name}.npz"
    hftbt_path = out_dir / "hftbt.npz"
    snapshot_path = out_dir / "hftbt_snapshot.npz"

    research_arr = np.asarray(research_rows, dtype=_RESEARCH_DTYPE)
    hftbt_arr = np.asarray(hftbt_rows, dtype=_HBT_EVENT_DTYPE)
    snapshot_arr = np.asarray(snapshot_rows, dtype=_HBT_EVENT_DTYPE)
    np.savez_compressed(str(research_path), data=research_arr)
    np.savez_compressed(str(hftbt_path), data=hftbt_arr)
    np.savez_compressed(str(snapshot_path), data=snapshot_arr)

    parameters = {"price_scale": int(price_scale), "source_name": source}
    paper_ref_values = [str(item) for item in (paper_refs or [])]
    symbols = [resolved_symbol]
    research_meta = _build_meta(
        path=research_path,
        rows=int(research_arr.shape[0]),
        fields=_RESEARCH_DTYPE.names or (),
        fingerprint=_sha256_file(research_path),
        owner=owner,
        source_name=source,
        source_type=source_type,
        split=split,
        symbols=symbols,
        parameters=parameters,
    )
    research_meta.update(
        {
            "bundle": {
                "backtest_data": str(hftbt_path),
                "backtest_snapshot": str(snapshot_path),
                "layout": "research_hftbt_bundle_v1",
                "primary_data": str(research_path),
            },
            "dataset_id": f"{research_path.stem}_v1",
            "paper_refs": paper_ref_values,
        }
    )
    hftbt_meta = _build_meta(
        path=hftbt_path,
        rows=int(hftbt_arr.shape[0]),
        fields=_HBT_EVENT_DTYPE.names or (),
        fingerprint=_sha256_file(hftbt_path),
        owner=owner,
        source_name=source,
        source_type=source_type,
        split="full",
        symbols=symbols,
        parameters=parameters,
    )
    hftbt_meta["dataset_id"] = "hftbt_v1"
    snapshot_meta = _build_meta(
        path=snapshot_path,
        rows=int(snapshot_arr.shape[0]),
        fields=_HBT_EVENT_DTYPE.names or (),
        fingerprint=_sha256_file(snapshot_path),
        owner=owner,
        source_name=source,
        source_type=source_type,
        split="full",
        symbols=symbols,
        parameters=parameters,
    )
    snapshot_meta["dataset_id"] = "hftbt_snapshot_v1"

    research_meta_path = _meta_path_for(research_path)
    hftbt_meta_path = _meta_path_for(hftbt_path)
    snapshot_meta_path = _meta_path_for(snapshot_path)
    _write_json(research_meta_path, research_meta)
    _write_json(hftbt_meta_path, hftbt_meta)
    _write_json(snapshot_meta_path, snapshot_meta)
    audit_payload = audit_governed_bundle(research_path)
    audit_path = research_path.parent / "bundle_audit.json"
    if "source_meta" not in audit_payload["bundle"]:
        _write_json(audit_path, audit_payload)

    return GovernedBundle(
        primary_data=research_path,
        hftbt_path=hftbt_path,
        hftbt_snapshot_path=snapshot_path,
        primary_meta=research_meta_path,
        hftbt_meta=hftbt_meta_path,
        hftbt_snapshot_meta=snapshot_meta_path,
        bundle_audit=audit_path,
    )


def prepare_governed_data(args: argparse.Namespace) -> dict[str, str]:
    paper_refs = [str(item) for item in getattr(args, "paper_ref", [])]
    if not paper_refs and getattr(args, "paper_refs", None):
        paper_refs = [str(item) for item in args.paper_refs]
    bundle = prepare_clickhouse_export(
        input_path=args.input,
        output_dir=args.out_dir,
        alpha_id=args.alpha_id,
        owner=args.owner,
        split=args.split,
        symbol=getattr(args, "symbol", None),
        tag=getattr(args, "tag", None),
        source=args.source,
        source_type=getattr(args, "source_type", "real"),
        price_scale=float(args.price_scale),
        limit=getattr(args, "limit", None),
        paper_refs=paper_refs,
        chunk_size=getattr(args, "chunk_size", 50_000),
    )
    return bundle.to_dict()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare governed research bundle from historical data.")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare-governed-data", help="Convert input data into governed research bundle")
    prepare.add_argument("--alpha-id", required=True)
    prepare.add_argument("--input", required=True)
    prepare.add_argument("--out-dir", required=True)
    prepare.add_argument("--symbol", default=None)
    prepare.add_argument("--tag", default=None)
    prepare.add_argument("--split", default="full")
    prepare.add_argument("--owner", default="research")
    prepare.add_argument("--source", default="clickhouse_export")
    prepare.add_argument("--source-type", default="real", choices=["real", "synthetic"])
    prepare.add_argument("--price-scale", type=float, default=1_000_000.0)
    prepare.add_argument("--limit", type=int, default=None)
    prepare.add_argument("--chunk-size", type=int, default=50_000)
    prepare.add_argument("--paper-ref", action="append", default=[])
    prepare.set_defaults(func=prepare_governed_data)

    audit = sub.add_parser("audit-governed-bundle", help="Audit an existing governed bundle")
    audit.add_argument("path")
    audit.set_defaults(func=lambda ns: audit_governed_bundle(ns.path))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = args.func(args)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
