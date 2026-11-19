"""Unified BacktestResult dataclass for both maker and taker strategies.

Fields common to both strategy types are required; strategy-specific metrics
default to None on the irrelevant side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np


@dataclass(frozen=True)
class BacktestResult:
    """Unified backtest result for maker and taker strategies."""

    run_id: str
    config_hash: str
    instrument: str
    strategy_name: str
    strategy_type: Literal["maker", "taker"]

    engine: str
    queue_model: str
    calibration_profile_id: str
    data_source: str
    latency_profile: str

    pnl_pts: float
    n_fills: int
    n_trading_days: int
    equity_curve: np.ndarray

    pnl_per_fill: float | None = None
    adverse_fill_pct: float | None = None
    fill_rate_per_day: float | None = None

    ic_is: float | None = None
    ic_oos: float | None = None

    daily_pnl: list[float] = field(default_factory=list)

    replay_parity_report: Any | None = None

    def to_provenance_dict(self) -> dict:
        """Serializable provenance (excludes large arrays)."""
        return {
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "instrument": self.instrument,
            "strategy_name": self.strategy_name,
            "strategy_type": self.strategy_type,
            "engine": self.engine,
            "queue_model": self.queue_model,
            "calibration_profile_id": self.calibration_profile_id,
            "data_source": self.data_source,
            "latency_profile": self.latency_profile,
            "pnl_pts": self.pnl_pts,
            "n_fills": self.n_fills,
            "n_trading_days": self.n_trading_days,
            "pnl_per_fill": self.pnl_per_fill,
            "adverse_fill_pct": self.adverse_fill_pct,
            "fill_rate_per_day": self.fill_rate_per_day,
            "ic_is": self.ic_is,
            "ic_oos": self.ic_oos,
        }
