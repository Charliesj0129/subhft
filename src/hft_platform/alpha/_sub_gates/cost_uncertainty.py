"""CostUncertaintyGate — fail when P95 lower bound of daily PnL <= threshold.

Slice B introduces a statistical-uncertainty gate on per-day PnL. A maker or
taker alpha that posts a positive headline number across N days but with high
day-to-day variance is statistically indistinguishable from a zero-edge alpha
under cost noise. This gate enforces:

    P95_lower_bound = mean_daily_pnl - 1.645 * std_daily_pnl / sqrt(n_days)

If ``P95_lower_bound <= cost_uncertainty_p95_lower_bound_min_pts`` (default 0.0
under strict profile), the gate FAILS.

Threshold key: ``cost_uncertainty_p95_lower_bound_min_pts`` (in
``config/research/profiles/vm_ul6_strict.yaml :: thresholds.{maker,taker}``).
When the threshold is absent, the gate returns advisory PASS (loose-profile
semantics, mirroring ``InventoryMtMGate``).

Slice B (2026-05-05): pivoted from the original
``Scorecard.cost_sensitivity_ratio`` design after that field was found to be
unreliably populated on ``BacktestResult`` (see Task 9 follow-up). A statistical
CI on ``daily_pnl`` is the simpler, contract-stable equivalent and lets the gate
share Task 9's input contract exactly.
"""

from __future__ import annotations

import math
from statistics import mean, stdev
from typing import Any

from hft_platform.alpha._sub_gates.registry import SubGateResult

# One-sided 95% normal CI z-score.
_Z_95_ONE_SIDED: float = 1.645


class CostUncertaintyGate:
    """Block promotion when the daily-PnL CI lower bound is at/below the floor.

    Reads each row of ``result.daily_pnl`` for keys:
      * ``pnl_pts`` — realized PnL for the day in points.
      * ``fills`` — number of completed fills for the day. Days with zero
        fills are excluded from the statistical series (weekends/holidays).

    Then computes mean, sample std (ddof=1), SEM, and P95 lower bound.
    """

    name: str = "cost_uncertainty"
    applies_to = {"maker", "taker"}

    def evaluate(
        self,
        result: Any,
        config: Any,
        thresholds: dict,
    ) -> SubGateResult:
        floor = thresholds.get("cost_uncertainty_p95_lower_bound_min_pts")
        is_strict = bool(thresholds.get("_is_strict_profile", False))
        # Minimum sample size under strict profile: variance from <5 days
        # is too unstable to gate promotion on.  Loose profile keeps n>=2
        # to preserve the back-compat advisory branch.
        min_n_strict = int(thresholds.get("cost_uncertainty_min_days_strict", 5))

        daily = getattr(result, "daily_pnl", None) or []
        # Accept both Slice B dict rows ({pnl_pts, fills, ...}) and legacy
        # float rows (raw daily PnL). Float rows are assumed to be traded
        # days; dict rows with fills==0 are excluded as non-traded days.
        pnl_series: list[float] = []
        for row in daily:
            if isinstance(row, dict):
                if int(row.get("fills", 0) or 0) > 0:
                    pnl_series.append(float(row.get("pnl_pts", 0.0) or 0.0))
            else:
                pnl_series.append(float(row))

        n_days = len(pnl_series)

        # Strict profile: insufficient sample → FAIL.  Loose: keep advisory PASS.
        if (is_strict and n_days < min_n_strict) or n_days < 2:
            metrics_short: dict[str, Any] = {
                "n_days": n_days,
                "p95_lower_bound_pts": None,
                "mean_daily_pnl_pts": (round(pnl_series[0], 4) if pnl_series else 0.0),
                "std_daily_pnl_pts": None,
                "threshold_pts": float(floor) if floor is not None else None,
            }
            if is_strict and n_days < min_n_strict:
                return SubGateResult(
                    name=self.name,
                    passed=False,
                    metrics=metrics_short,
                    details=(
                        f"STRICT FAIL: n_days={n_days} < min_n_strict={min_n_strict} (insufficient sample for CI)"
                    ),
                )
            return SubGateResult(
                name=self.name,
                passed=True,
                metrics=metrics_short,
                details=f"advisory: n_days={n_days} < 2 (variance undefined)",
            )

        mu = mean(pnl_series)
        sigma = stdev(pnl_series)  # sample std (ddof=1)
        sem = sigma / math.sqrt(n_days)
        p95_lower = mu - _Z_95_ONE_SIDED * sem

        if floor is None:
            metrics_advisory: dict[str, Any] = {
                "n_days": n_days,
                "p95_lower_bound_pts": round(p95_lower, 4),
                "mean_daily_pnl_pts": round(mu, 4),
                "std_daily_pnl_pts": round(sigma, 4),
                "threshold_pts": None,
            }
            prefix = "STRICT FAIL" if is_strict else "advisory"
            return SubGateResult(
                name=self.name,
                passed=not is_strict,
                metrics=metrics_advisory,
                details=(
                    f"{prefix}: cost_uncertainty_p95_lower_bound_min_pts threshold "
                    f"absent (lower_bound={p95_lower:.4f})"
                ),
            )

        threshold = float(floor)
        passed = p95_lower > threshold
        details = "OK" if passed else (f"P95 lower bound={p95_lower:.4f} <= threshold={threshold:.4f}")

        metrics_full: dict[str, Any] = {
            "n_days": n_days,
            "p95_lower_bound_pts": round(p95_lower, 4),
            "mean_daily_pnl_pts": round(mu, 4),
            "std_daily_pnl_pts": round(sigma, 4),
            "threshold_pts": threshold,
        }
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics=metrics_full,
            details=details,
        )
