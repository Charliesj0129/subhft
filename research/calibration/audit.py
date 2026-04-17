"""Data audit tool for calibration: inventory live fills across sources.

Produces a structured report of what fill data exists per instrument,
including quality flags indicating missing queue position, decision price,
or sparse coverage.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

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
        if len(parts) < 2:
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
