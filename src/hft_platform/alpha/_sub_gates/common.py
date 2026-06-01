"""Common sub-gates applicable to both maker and taker strategies."""

from __future__ import annotations

from statistics import mean, stdev
from typing import Any

import numpy as np

from hft_platform.alpha._sub_gates.registry import SubGateResult


def _entry_to_float(entry: Any) -> float:
    """Coerce a ``daily_pnl`` row to its scalar net PnL value.

    ``BacktestResult.daily_pnl`` was historically a ``list[float]``; the
    canonical shape (per ``research/backtest/types.py``) is now
    ``list[dict]`` with ``pnl_pts`` and the per-day audit fields.  Strict
    sub-gates (``inventory_mtm``, ``monthly_distribution``) require dict
    shape; legacy ones (``sharpe_threshold``, ``max_drawdown``,
    ``winning_day_pct``) used to require float shape.  This helper
    bridges both so the same fixture/payload feeds every gate.
    """
    if isinstance(entry, dict):
        return float(entry.get("pnl_pts", 0.0))
    return float(entry)


def _to_float_list(daily: Any) -> list[float]:
    return [_entry_to_float(e) for e in (daily or [])]


class SharpeThresholdGate:
    """Daily Sharpe ratio threshold check (annualized by sqrt(252))."""

    name = "sharpe_threshold"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        pnl = _to_float_list(result.daily_pnl)
        if len(pnl) < 2:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={"sharpe": 0.0, "n_days": float(len(pnl))},
                details="insufficient days for Sharpe (need >= 2)",
            )
        m = mean(pnl)
        s = stdev(pnl)
        sharpe = (m / s * np.sqrt(252)) if s > 0 else 0.0
        min_sharpe = thresholds.get("sharpe_is_min", 0.5)
        passed = sharpe >= min_sharpe
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={"sharpe": float(sharpe), "threshold": float(min_sharpe)},
            details=f"sharpe={sharpe:.2f} vs min {min_sharpe}",
        )


class MaxDrawdownGate:
    """Maximum drawdown threshold check (as % of running peak)."""

    name = "max_drawdown"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        pnl = _to_float_list(result.daily_pnl)
        if not pnl:
            return SubGateResult(
                name=self.name,
                passed=True,
                metrics={"max_dd_pct": 0.0},
                details="no daily pnl to evaluate",
            )
        equity = np.cumsum(pnl)
        running_max = np.maximum.accumulate(equity)
        drawdown = running_max - equity
        max_dd = float(drawdown.max()) if len(drawdown) else 0.0
        peak = float(running_max.max()) if len(running_max) else 0.0
        max_dd_pct = (max_dd / peak * 100.0) if peak > 0 else 0.0

        threshold_pct = thresholds.get("max_drawdown_pct", 30.0)
        passed = max_dd_pct <= threshold_pct
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={"max_dd_pct": float(max_dd_pct), "threshold": float(threshold_pct)},
            details=f"max_dd={max_dd_pct:.1f}% vs max {threshold_pct}%",
        )


class WinningDayPctGate:
    """Winning-day percentage threshold check (PnL > 0 counts as win)."""

    name = "winning_day_pct"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        pnl = _to_float_list(result.daily_pnl)
        if not pnl:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={"winning_day_pct": 0.0},
                details="no daily pnl to evaluate",
            )
        n_wins = sum(1 for p in pnl if p > 0)
        pct = n_wins / len(pnl) * 100.0
        threshold = thresholds.get("winning_day_pct_min", 55.0)
        passed = pct >= threshold
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={"winning_day_pct": float(pct), "threshold": float(threshold)},
            details=f"winning_day={pct:.1f}% vs min {threshold}%",
        )
