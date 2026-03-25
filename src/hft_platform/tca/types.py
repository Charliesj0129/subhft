"""TCA data types for fee calculation and transaction cost analysis."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class FeeSchedule:
    """Fee schedule configuration for a symbol."""

    symbol: str
    commission_per_contract: int  # NTD per contract
    tax_rate_bps: float           # Tax rate in basis points
    tax_side: str                  # "sell", "both"
    tick_size: float               # Minimum price increment
    point_value: int               # NTD per point


@dataclass(slots=True)
class FeeBreakdown:
    """Calculated fee breakdown for a fill. All values in NTD scaled x10000."""

    commission: int
    tax: int
    total: int
