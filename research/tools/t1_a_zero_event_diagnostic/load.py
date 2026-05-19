"""Coverage CSV loader and viability event counter."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

DEDUPE_KEY = ("contract", "trading_day")


def csv_sha256(path: str | Path) -> str:
    """Return sha256 for a CSV or sidecar path."""
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_one(path: Path) -> pd.DataFrame:
    if path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _dedupe(df: pd.DataFrame, *, source_order: list[str]) -> pd.DataFrame:
    """Dedupe on ``(contract, trading_day)`` using the spec tie-breakers."""
    if df.empty:
        return df
    order_rank = {p: i for i, p in enumerate(source_order)}
    out = df.copy()
    out["_bbo_last_dt"] = pd.to_datetime(
        out["bbo_last_time"], errors="coerce", utc=True
    )
    out["_source_rank"] = out["_source_path"].map(order_rank).fillna(-1).astype(int)
    out["_has_bbo_dt"] = out["_bbo_last_dt"].notna().astype(int)
    out = out.sort_values(
        by=list(DEDUPE_KEY) + ["_has_bbo_dt", "_bbo_last_dt", "_source_rank"],
        ascending=[True, True, True, True, True],
        kind="mergesort",
    )
    out = out.drop_duplicates(subset=list(DEDUPE_KEY), keep="last")
    return out.drop(
        columns=["_bbo_last_dt", "_source_rank", "_has_bbo_dt", "_source_path"],
        errors="ignore",
    )


def load_and_dedupe_coverage(paths: list[Path]) -> tuple[pd.DataFrame, dict[str, str]]:
    """Read, concatenate, and dedupe coverage CSVs."""
    if not paths:
        raise ValueError("no coverage rows: empty input list")
    frames: list[pd.DataFrame] = []
    sha_map: dict[str, str] = {}
    source_order: list[str] = []
    for path in paths:
        p = Path(path)
        sha_map[str(p)] = csv_sha256(p)
        source_order.append(str(p))
        sub = _read_one(p)
        if not sub.empty:
            sub = sub.copy()
            sub["_source_path"] = str(p)
            frames.append(sub)
    if not frames:
        raise ValueError("no coverage rows: all input files empty")
    concat = pd.concat(frames, ignore_index=True)
    deduped = _dedupe(concat, source_order=source_order)
    return deduped.reset_index(drop=True), sha_map


def read_viability_event_count(path: str | Path) -> int:
    """Return event rows in a viability event CSV; empty/header-only means zero."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    if p.stat().st_size == 0:
        return 0
    try:
        df = pd.read_csv(p)
    except pd.errors.EmptyDataError:
        return 0
    return int(len(df))


def read_summary_event_count(path: str | Path) -> int | None:
    """Return summary JSON ``events`` when present."""
    p = Path(path)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    events = data.get("events")
    return None if events is None else int(events)


def find_summary_sibling(path: str | Path) -> Path | None:
    """Find the likely summary JSON sibling for a viability event CSV."""
    p = Path(path)
    candidates = [
        p.with_name(p.name.replace("_opening_range_events.csv", "_summary.json")),
        p.with_name(p.stem.replace("_opening_range_events", "_summary") + ".json"),
    ]
    for candidate in candidates:
        if candidate != p and candidate.exists():
            return candidate
    matches = sorted(p.parent.glob("*_summary.json"))
    return matches[-1] if matches else None


def freshness_check(df: pd.DataFrame, viability_events_csv: str | Path) -> dict:
    """Compare deduped coverage row count to sibling viability summary days."""
    summary_path = find_summary_sibling(viability_events_csv)
    input_days = int(len(df))
    result = {
        "summary_path": str(summary_path) if summary_path else None,
        "audited_trading_days_summary": None,
        "audited_trading_days_in_input": input_days,
        "match": None,
    }
    if summary_path is None:
        return result
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    summary_days = data.get("audited_trading_days")
    if summary_days is None:
        return result
    result["audited_trading_days_summary"] = int(summary_days)
    result["match"] = int(summary_days) == input_days
    return result
