"""Leave-one-day-out sensitivity sub-gate.

When the sign of total PnL flips after removing any single day, the
edge is single-day-dominated and the gate fails.
"""

from __future__ import annotations

from typing import Any

from hft_platform.alpha._resampling import leave_one_day_out
from hft_platform.alpha._sub_gates.registry import SubGateResult


def _entry_to_float(entry: Any) -> float:
    if isinstance(entry, dict):
        return float(entry.get("pnl_pts", 0.0))
    return float(entry)


class LOODaySensitivityGate:
    """Sign of total PnL must survive any single-day removal."""

    name = "loo_day_sensitivity"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        require = bool(thresholds.get("loo_day_sign_preserved", False))
        if not require:
            return SubGateResult(
                name=self.name,
                passed=True,
                metrics={},
                details="loo_day_sign_preserved=False — gate skipped",
            )

        daily = [_entry_to_float(e) for e in (getattr(result, "daily_pnl", None) or [])]
        if len(daily) < 2:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={"n_days": float(len(daily))},
                details=f"insufficient days for LOO analysis: n={len(daily)}, need >= 2",
            )

        total = sum(daily)
        target_sign = 1 if total > 0 else (-1 if total < 0 else 0)
        # Track the LOO sum most threatening to sign preservation.
        # "Most threatening" = smallest value of (s * target_sign), i.e. the
        # LOO sum furthest in the direction opposite to total.
        loo_sums = [sum(sliced) for sliced in leave_one_day_out(daily)]
        worst = min(loo_sums, key=lambda s: s * target_sign) if target_sign != 0 else loo_sums[0]
        worst_sign = 1 if worst > 0 else (-1 if worst < 0 else 0)
        passed = (target_sign == worst_sign) and target_sign != 0

        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "total_pnl": float(total),
                "worst_loo_pnl": float(worst),
                "n_days": float(len(daily)),
            },
            details=(f"total={total:.2f}, worst LOO={worst:.2f} (sign-preserved={passed})"),
        )
