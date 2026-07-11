"""Split definition loading (``split_definition_version = split_v1``, spec §5/§7).

The split file pins explicit (day, symbol) pairs — front-month TXF per day —
so evaluation is reproducible against a fixed NPZ inventory
(``data_version``).  Splits are contiguous in time and day-disjoint; the test
split is only ever touched for WATCHLIST/PROMOTED candidates (spec §14) and
never appears in failure summaries (spec §15).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

SPLIT_NAMES = ("train", "validation", "test")


@dataclass(frozen=True)
class DaySymbol:
    day: str  # 'YYYY-MM-DD'
    symbol: str  # e.g. 'TXFD6'


@dataclass(frozen=True)
class SplitDefinition:
    split_definition_version: str
    data_version: str
    splits: dict[str, tuple[DaySymbol, ...]]

    def split_range(self, name: str) -> tuple[str, str]:
        """(first_day, last_day) of a split."""
        days = [ds.day for ds in self.splits[name]]
        return min(days), max(days)

    def all_pairs(self) -> list[tuple[str, DaySymbol]]:
        """(split_name, pair) for every pair, in declared order."""
        return [(name, ds) for name in SPLIT_NAMES for ds in self.splits.get(name, ())]


def load_split_definition(path: Path) -> SplitDefinition:
    raw = yaml.safe_load(path.read_text())
    splits: dict[str, tuple[DaySymbol, ...]] = {}
    for name, pairs in raw["splits"].items():
        if name not in SPLIT_NAMES:
            raise ValueError(f"Unknown split {name!r} (want one of {SPLIT_NAMES})")
        splits[name] = tuple(DaySymbol(day=str(p["day"]), symbol=str(p["symbol"])) for p in pairs)
    definition = SplitDefinition(
        split_definition_version=str(raw["split_definition_version"]),
        data_version=str(raw["data_version"]),
        splits=splits,
    )
    _validate(definition)
    return definition


def _validate(definition: SplitDefinition) -> None:
    seen_days: dict[str, str] = {}
    for name in SPLIT_NAMES:
        pairs = definition.splits.get(name, ())
        if not pairs:
            raise ValueError(f"Split {name!r} is empty or missing")
        for ds in pairs:
            if ds.day in seen_days:
                raise ValueError(f"Day {ds.day} appears in both {seen_days[ds.day]!r} and {name!r}")
            seen_days[ds.day] = name


def npz_path_for(data_root: Path, ds: DaySymbol) -> Path:
    """Inventory convention: ``<root>/<symbol_lower>/<SYMBOL>_<day>_l2.hftbt.npz``."""
    return data_root / ds.symbol.lower() / f"{ds.symbol}_{ds.day}_l2.hftbt.npz"


__all__ = [
    "SPLIT_NAMES",
    "DaySymbol",
    "SplitDefinition",
    "load_split_definition",
    "npz_path_for",
]
