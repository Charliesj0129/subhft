"""Per-instrument cost models for standardized backtest engine."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import yaml

from hft_platform.alpha.paths import repo_path


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


# Relative to the repo root, not the cwd (see ``hft_platform.alpha.paths``).
_CONFIG_PATH = Path("config/research/cost_profiles.yaml")
# Keyed by the resolved file, so a different file is never served from another's cache.
_cache: dict[Path, dict[str, TAIFEXCost]] = {}


def _load_all() -> dict[str, TAIFEXCost]:
    path = repo_path(_CONFIG_PATH)
    cached = _cache.get(path)
    if cached is not None:
        return cached
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    profiles: dict[str, TAIFEXCost] = {}
    for instrument, vals in raw.items():
        profiles[instrument] = TAIFEXCost(
            instrument=instrument,
            commission_pts_per_side=float(vals["commission_pts_per_side"]),
            tax_pts_per_side=float(vals["tax_pts_per_side"]),
            point_value_nwd=int(vals["point_value_nwd"]),
            scale=int(vals.get("scale", 1_000_000)),
        )
    _cache[path] = profiles
    return profiles


# TAIFEX month codes A-L (Jan-Dec) plus the year digit: TMFJ6 is the October 2026 TMF contract.
_CONTRACT_MONTH_CODE = re.compile(r"^(?P<root>TXF|MXF|TMF)[A-L]\d$")


def load_cost_profile(instrument: str) -> TAIFEXCost:
    """Cost profile for ``instrument``: an exact key, else the root of a contract-month code.

    The root profile (``TXF`` / ``MXF`` / ``TMF``) is the conservative default, so a month
    nobody listed (``TMFJ6``) is still costed rather than raising or silently being free.
    """
    profiles = _load_all()
    if instrument in profiles:
        return profiles[instrument]
    match = _CONTRACT_MONTH_CODE.match(instrument)
    if match and match.group("root") in profiles:
        return replace(profiles[match.group("root")], instrument=instrument)
    raise KeyError(
        f"No cost profile for '{instrument}'. "
        f"Available: {sorted(profiles.keys())}. "
        f"Add to {repo_path(_CONFIG_PATH)}"
    )
