"""Single-day-dominance sub-gate.

Rejects runs where the top-magnitude day contributes more than
``outlier_day_contribution_max_pct`` percent of the absolute total
daily PnL.
"""

from __future__ import annotations

from typing import Any

from hft_platform.alpha._sub_gates.common import _to_daily_series
from hft_platform.alpha._sub_gates.registry import SubGateResult


class SingleDayDominanceGate:
    """Reject when one day's |PnL| / sum(|daily PnL|) exceeds threshold."""

    name = "single_day_dominance"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        daily = _to_daily_series(getattr(result, "daily_pnl", None))
        max_pct = float(thresholds.get("outlier_day_contribution_max_pct", 100.0))

        if not daily:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={"top_day_contribution_pct": 0.0, "threshold_pct": max_pct},
                details="no daily pnl to evaluate",
            )

        abs_total = sum(abs(d) for d in daily)
        if abs_total <= 0.0:
            return SubGateResult(
                name=self.name,
                passed=True,
                metrics={"top_day_contribution_pct": 0.0, "threshold_pct": max_pct},
                details="no measurable PnL — gate skipped",
            )

        top = max(abs(d) for d in daily)
        pct = top / abs_total * 100.0
        passed = pct <= max_pct
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "top_day_contribution_pct": float(pct),
                "threshold_pct": float(max_pct),
                "n_days": float(len(daily)),
            },
            details=f"top_day={pct:.1f}% of |total| (max {max_pct:.1f}%)",
        )
