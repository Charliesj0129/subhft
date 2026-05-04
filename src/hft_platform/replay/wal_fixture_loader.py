"""Load .wal/archive tar.gz fixtures into ordered MarketDataEvent dicts.

Public API:
    load_market_data_events(path, symbols=None) -> Iterator[dict]

Each yielded dict is a single market_data row (BidAsk or Tick) from the
WAL archive, sorted by exch_ts ascending across all shards.
"""
from __future__ import annotations

import json
import tarfile
from collections.abc import Iterator
from pathlib import Path


class FixtureLoadError(RuntimeError):
    pass


def _iter_shard_rows(reader) -> Iterator[dict]:
    """Yield body rows from a single jsonl shard. Skips header (first line)."""
    first = True
    for raw in reader:
        line = raw.strip()
        if not line:
            continue
        if first:
            first = False
            try:
                header = json.loads(line)
            except Exception:
                return
            if header.get("__wal_table__") != "hft.market_data":
                return
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def load_market_data_events(
    path: str | Path,
    *,
    symbols: set[str] | None = None,
) -> Iterator[dict]:
    """Yield market_data rows from a .tar.gz WAL fixture, sorted by exch_ts."""
    p = Path(path)
    if not p.exists():
        raise FixtureLoadError(f"fixture not found: {p}")
    rows: list[dict] = []
    with tarfile.open(p, "r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            if not member.name.endswith(".jsonl"):
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            for row in _iter_shard_rows(line.decode("utf-8") for line in f):
                if symbols is not None and row.get("symbol") not in symbols:
                    continue
                rows.append(row)
    rows.sort(key=lambda r: int(r.get("exch_ts", 0)))
    yield from rows
