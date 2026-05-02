"""Per-instrument cost models for standardized backtest engine."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml


class CostModel(Protocol):
    """Protocol for cost models."""

    @property
    def rt_cost_pts(self) -> float: ...

    @property
    def label(self) -> str: ...

    def apply(self, gross_pnl_pts: float, n_fills: int) -> float: ...


@dataclass(frozen=True)
class TAIFEXCost:
    """TAIFEX futures cost model."""

    instrument: str
    commission_pts_per_side: float
    tax_pts_per_side: float
    point_value_nwd: int
    scale: int = 1_000_000

    @property
    def cost_per_side_pts(self) -> float:
        return self.commission_pts_per_side + self.tax_pts_per_side

    @property
    def rt_cost_pts(self) -> float:
        return 2 * self.cost_per_side_pts

    @property
    def label(self) -> str:
        return (
            f"{self.instrument}"
            f"(comm={self.commission_pts_per_side},tax={self.tax_pts_per_side})"
        )

    def apply(self, gross_pnl_pts: float, n_fills: int) -> float:
        return gross_pnl_pts - n_fills * self.cost_per_side_pts


_CONFIG_PATH = Path("config/research/cost_profiles.yaml")
_cache: dict[str, TAIFEXCost] | None = None


def _load_all() -> dict[str, TAIFEXCost]:
    global _cache
    if _cache is not None:
        return _cache
    raw: dict[str, Any] = yaml.safe_load(_CONFIG_PATH.read_text())
    _cache = {}
    for instrument, vals in raw.items():
        _cache[instrument] = TAIFEXCost(
            instrument=instrument,
            commission_pts_per_side=float(vals["commission_pts_per_side"]),
            tax_pts_per_side=float(vals["tax_pts_per_side"]),
            point_value_nwd=int(vals["point_value_nwd"]),
            scale=int(vals.get("scale", 1_000_000)),
        )
    return _cache


def load_cost_profile(instrument: str) -> TAIFEXCost:
    profiles = _load_all()
    if instrument not in profiles:
        raise KeyError(
            f"No cost profile for '{instrument}'. "
            f"Available: {sorted(profiles.keys())}. "
            f"Add to {_CONFIG_PATH}"
        )
    return profiles[instrument]
