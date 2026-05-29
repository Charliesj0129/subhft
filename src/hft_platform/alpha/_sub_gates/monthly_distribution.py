"""Monthly-distribution sub-gate.

Enforces goal §6 (research validation criteria) at the month level — the
existing day-level gates (``single_day_dominance``, ``loo_day_sensitivity``,
``outlier_trade_removal``) leave a gap at the month-level concentration and
drawdown-to-monthly-PnL relationship.

Two hard checks per evaluation, both must pass:

1. ``top_month_contribution_pct <= top_month_contribution_max_pct`` — a
   single calendar month must not contribute more than the configured
   share of total |monthly PnL|.
2. ``max_drawdown_pts <= drawdown_to_avg_monthly_max_ratio *
   avg_monthly_net_pnl_pts`` — peak-to-trough drawdown on the monthly
   equity curve must not exceed N× average monthly net PnL.  If
   ``avg_monthly_net_pnl_pts`` is non-positive, the gate fails: there is
   no positive monthly baseline against which the drawdown is tolerable.

The gate also records ``median_monthly_net_pnl_pts`` and
``worst_monthly_pnl_pts`` for downstream audit (goal §6 explicitly asks
to surface both alongside the dominance check).
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any

from hft_platform.alpha._sub_gates.registry import SubGateResult


def _pnl(entry: Any) -> float:
    if isinstance(entry, dict):
        return float(entry.get("pnl_pts", 0.0))
    return float(entry)


def _month_key(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    date = entry.get("date")
    if not isinstance(date, str) or len(date) < 7:
        return None
    return date[:7]  # "YYYY-MM"


def _max_drawdown(equity: list[float]) -> float:
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > mdd:
            mdd = dd
    return mdd


class MonthlyDistributionGate:
    """Reject runs where monthly PnL distribution fails goal §6 thresholds."""

    name = "monthly_distribution"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        daily = list(getattr(result, "daily_pnl", None) or [])
        top_max = float(thresholds.get("top_month_contribution_max_pct", 50.0))
        dd_max_ratio = float(thresholds.get("drawdown_to_avg_monthly_max_ratio", 2.0))
        min_months = int(thresholds.get("monthly_distribution_min_months", 2))

        # Aggregate by month, skipping entries without a usable date.
        monthly: dict[str, float] = defaultdict(float)
        for e in daily:
            key = _month_key(e)
            if key is None:
                continue
            monthly[key] += _pnl(e)

        n_months = len(monthly)
        if n_months == 0:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={
                    "n_months": 0.0,
                    "top_month_contribution_pct": 0.0,
                    "avg_monthly_net_pnl_pts": 0.0,
                    "median_monthly_net_pnl_pts": 0.0,
                    "worst_monthly_pnl_pts": 0.0,
                    "max_drawdown_pts": 0.0,
                    "drawdown_to_avg_monthly_ratio": 0.0,
                },
                details="no dated daily_pnl entries to evaluate",
            )

        ordered = sorted(monthly.items())  # chronological by YYYY-MM string
        values = [v for _, v in ordered]

        abs_total = sum(abs(v) for v in values)
        top_pct = (max(abs(v) for v in values) / abs_total * 100.0) if abs_total > 0 else 0.0

        avg_monthly = sum(values) / n_months
        med_monthly = float(median(values))
        worst_monthly = float(min(values))

        equity = []
        running = 0.0
        for v in values:
            running += v
            equity.append(running)
        mdd = _max_drawdown(equity)
        if avg_monthly > 0:
            dd_ratio = mdd / avg_monthly
        else:
            dd_ratio = float("inf")

        metrics = {
            "n_months": float(n_months),
            "top_month_contribution_pct": float(top_pct),
            "top_month_contribution_max_pct": float(top_max),
            "avg_monthly_net_pnl_pts": float(avg_monthly),
            "median_monthly_net_pnl_pts": med_monthly,
            "worst_monthly_pnl_pts": worst_monthly,
            "max_drawdown_pts": float(mdd),
            "drawdown_to_avg_monthly_ratio": float(dd_ratio),
            "drawdown_to_avg_monthly_max_ratio": float(dd_max_ratio),
        }

        if n_months < min_months:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics=metrics,
                details=(f"insufficient months: {n_months} < {min_months} required"),
            )

        reasons: list[str] = []
        if top_pct > top_max:
            reasons.append(f"top_month={top_pct:.1f}% > {top_max:.1f}%")
        if avg_monthly <= 0:
            reasons.append(f"avg_monthly={avg_monthly:.2f} pts <= 0 (no positive baseline)")
        elif dd_ratio > dd_max_ratio:
            reasons.append(
                f"mdd/avg_monthly={dd_ratio:.2f} > {dd_max_ratio:.2f} "
                f"(mdd={mdd:.1f} pts, avg_monthly={avg_monthly:.2f} pts)"
            )

        if reasons:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics=metrics,
                details="; ".join(reasons),
            )

        return SubGateResult(
            name=self.name,
            passed=True,
            metrics=metrics,
            details=(
                f"n_months={n_months}, top_month={top_pct:.1f}%, "
                f"mdd/avg_monthly={dd_ratio:.2f}, "
                f"median={med_monthly:.2f}, worst={worst_monthly:.2f}"
            ),
        )
