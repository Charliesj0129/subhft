"""Data audit tool for calibration: inventory live fills across sources.

Produces a structured report of what fill data exists per instrument,
including quality flags indicating missing queue position, decision price,
or sparse coverage.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class InstrumentAuditResult:
    """Per-instrument audit summary from a single data source."""

    instrument: str
    source: str
    date_range: tuple[str, str]
    n_trading_days: int
    n_fills: int
    n_fills_with_queue_position: int
    n_fills_with_decision_price: int
    n_fills_with_latency: int
    fill_rate_per_day: float
    instruments_found: list[str] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def audit_ck_export_parquet(directory: Path) -> list[InstrumentAuditResult]:
    """Audit CK export parquet files in a directory.

    Expected filename pattern: <INSTRUMENT>_<YYYY-MM-DD>.parquet
    Returns one InstrumentAuditResult per instrument found.
    """
    directory = Path(directory)
    if not directory.exists():
        return []

    files = sorted(directory.glob("*.parquet"))
    if not files:
        return []

    per_instrument: dict[str, list[tuple[pd.DataFrame, str, str]]] = {}
    for f in files:
        parts = f.stem.split("_")
        if len(parts) < 2 or not _DATE_RE.match(parts[1]):
            continue
        instrument = parts[0]
        date = parts[1]
        try:
            df = pd.read_parquet(f)
        except Exception:
            continue
        per_instrument.setdefault(instrument, []).append((df, date, f.name))

    results: list[InstrumentAuditResult] = []
    for instrument, entries in per_instrument.items():
        dates = sorted(e[1] for e in entries)
        total_rows = sum(len(e[0]) for e in entries)
        fill_cols: set[str] = set()
        for df, _, _ in entries:
            fill_cols.update(df.columns)

        has_queue_pos = "queue_position" in fill_cols
        has_decision_price = "decision_price" in fill_cols or "arrival_price" in fill_cols
        has_latency = "ts_exchange" in fill_cols and "ts_local" in fill_cols

        n_with_qp = total_rows if has_queue_pos else 0
        n_with_dp = total_rows if has_decision_price else 0
        n_with_lat = total_rows if has_latency else 0

        flags: list[str] = []
        if not has_queue_pos:
            flags.append("missing_queue_pos")
        if not has_decision_price:
            flags.append("missing_decision_price")
        if total_rows < 5 * len(entries):
            flags.append("sparse_data")

        results.append(InstrumentAuditResult(
            instrument=instrument,
            source="ck_export",
            date_range=(dates[0], dates[-1]),
            n_trading_days=len(entries),
            n_fills=total_rows,
            n_fills_with_queue_position=n_with_qp,
            n_fills_with_decision_price=n_with_dp,
            n_fills_with_latency=n_with_lat,
            fill_rate_per_day=total_rows / max(len(entries), 1),
            instruments_found=[instrument],
            quality_flags=flags,
        ))
    return results


def audit_clickhouse_fills(client: Any) -> list[InstrumentAuditResult]:
    """Audit hft.fills table in ClickHouse.

    Groups by symbol and counts trading days + fills.
    Returns empty list if no fills found or client is None.
    """
    if client is None:
        return []

    query = """
        SELECT
            symbol,
            toDate(toDateTime64(ts_exchange/1e9, 3)) AS trading_day,
            count() AS n_fills
        FROM hft.fills
        GROUP BY symbol, trading_day
        ORDER BY symbol, trading_day
    """
    df = client.query_df(query)
    if df.empty:
        return []

    results: list[InstrumentAuditResult] = []
    for instrument, group in df.groupby("symbol"):
        dates = sorted(group["trading_day"].astype(str).tolist())
        total_fills = int(group["n_fills"].sum())
        n_days = len(dates)

        flags: list[str] = []
        if total_fills / max(n_days, 1) < 5:
            flags.append("sparse_data")

        results.append(InstrumentAuditResult(
            instrument=instrument,
            source="ch_fills",
            date_range=(dates[0], dates[-1]),
            n_trading_days=n_days,
            n_fills=total_fills,
            n_fills_with_queue_position=0,
            n_fills_with_decision_price=0,
            n_fills_with_latency=total_fills,
            fill_rate_per_day=total_fills / max(n_days, 1),
            instruments_found=[instrument],
            quality_flags=flags,
        ))
    return results


def find_l2_data_days(data_dir: Path, instrument: str) -> list[str]:
    """Find trading days with L2 data for an instrument.

    Expected filename pattern: <INSTRUMENT>_<YYYY-MM-DD>_l2.hftbt.npz
    """
    data_dir = Path(data_dir)
    if not data_dir.exists():
        return []
    prefix = f"{instrument}_"
    suffix = "_l2.hftbt.npz"
    days: list[str] = []
    for f in data_dir.iterdir():
        name = f.name
        if name.startswith(prefix) and name.endswith(suffix):
            date = name[len(prefix):-len(suffix)]
            days.append(date)
    return sorted(days)


def audit_all(
    ck_export_dir: Path,
    l2_data_dir: Path,
    ch_client: Any = None,
) -> dict:
    """Run full audit across all sources and compute intersection with L2 data.

    Returns a structured report including usable calibration days per instrument.
    """
    ck_results = audit_ck_export_parquet(ck_export_dir)
    ch_results = audit_clickhouse_fills(ch_client)

    per_instrument: dict[str, dict] = {}
    for r in ck_results + ch_results:
        key = r.instrument
        bucket = per_instrument.setdefault(key, {
            "sources": [], "fill_dates": set(), "total_fills": 0,
        })
        bucket["sources"].append(r.to_dict())
        bucket["total_fills"] += r.n_fills
        bucket["fill_dates"].add(r.date_range[0])
        bucket["fill_dates"].add(r.date_range[1])

    for instrument, bucket in per_instrument.items():
        l2_days = set(find_l2_data_days(l2_data_dir, instrument))
        usable = sorted(bucket["fill_dates"] & l2_days)
        bucket["usable_calibration_days"] = usable
        bucket["n_usable_days"] = len(usable)
        bucket["fill_dates"] = sorted(bucket["fill_dates"])

    return {
        "per_instrument": per_instrument,
        "summary": {
            "total_instruments": len(per_instrument),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run calibration data audit")
    parser.add_argument("--ck-export-dir", type=Path,
                        default=Path("research/data/ck_export"))
    parser.add_argument("--l2-data-dir", type=Path,
                        default=Path("research/data/raw"))
    parser.add_argument("--ch-host", type=str, default="localhost")
    parser.add_argument("--ch-port", type=int, default=9000)
    parser.add_argument("--skip-clickhouse", action="store_true")
    parser.add_argument("--output", type=Path,
                        default=Path("research/calibration/artifacts/data_audit_report.json"))
    args = parser.parse_args()

    ch_client = None
    if not args.skip_clickhouse:
        try:
            import clickhouse_connect
            ch_client = clickhouse_connect.get_client(
                host=args.ch_host, port=args.ch_port,
            )
        except Exception as e:
            print(f"WARN: ClickHouse unavailable ({e}), skipping hft.fills audit",
                  file=sys.stderr)

    report = audit_all(
        ck_export_dir=args.ck_export_dir,
        l2_data_dir=args.l2_data_dir,
        ch_client=ch_client,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str))
    print(f"Audit report written to {args.output}")

    for instrument, bucket in report["per_instrument"].items():
        print(f"  {instrument}: {bucket['total_fills']} fills, "
              f"{bucket['n_usable_days']} usable calibration days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
