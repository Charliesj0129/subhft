"""TCA data structures. All monetary values NTD scaled x10000."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class FeeSchedule:
    """Fee schedule for a single futures product."""

    symbol: str
    commission_per_contract: int  # NTD (plain, not scaled)
    tax_rate_bps: float  # bps of notional, sell-side only
    tax_side: str  # "sell"
    tick_size: float
    point_value: int  # NTD per point


@dataclass(slots=True, frozen=True)
class FeeBreakdown:
    """Per-trade fee breakdown. All fields NTD scaled x10000."""

    commission: int
    tax: int
    total: int


@dataclass(slots=True, frozen=True)
class SlippageBreakdown:
    """Per-fill slippage decomposition. All fields in bps.

    WARNING: float fields — restricted to offline TCA analysis only.
    MUST NOT be used in live order-path code without converting to scaled int.
    """

    commission_bps: float
    tax_bps: float
    delay_cost_bps: float
    execution_cost_bps: float
    market_impact_bps: float
    total_bps: float


@dataclass(slots=True, frozen=True)
class TCADailyReport:
    """Aggregated TCA statistics for one (date, strategy, symbol) key."""

    date: str
    strategy: str
    symbol: str
    trade_count: int
    volume: int
    notional: int
    commission_bps_mean: float
    tax_bps_mean: float
    delay_cost_bps_mean: float
    delay_cost_bps_p95: float
    exec_cost_bps_mean: float
    exec_cost_bps_p95: float
    impact_bps_mean: float
    total_cost_bps_mean: float
    total_cost_bps_p95: float
