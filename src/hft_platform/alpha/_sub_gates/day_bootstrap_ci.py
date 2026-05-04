"""Day-level bootstrap CI sub-gate."""
from __future__ import annotations

from typing import Any

import numpy as np

from hft_platform.alpha._resampling import day_bootstrap
from hft_platform.alpha._sub_gates.registry import SubGateResult


def _entry_to_float(entry: Any) -> float:
    if isinstance(entry, dict):
        return float(entry.get("pnl_pts", 0.0))
    return float(entry)


class DayLevelBootstrapCIGate:
    """Bootstrap-mean lower CI bound on daily PnL must exceed threshold."""

    name = "day_bootstrap_ci"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        daily = [_entry_to_float(e) for e in (getattr(result, "daily_pnl", None) or [])]
        ci_min = float(thresholds.get("bootstrap_ci_lower_bound_min", 0.0))
        n_resamples = int(thresholds.get("bootstrap_n_resamples", 2000))
        alpha = float(thresholds.get("bootstrap_alpha", 0.05))
        seed = int(thresholds.get("bootstrap_rng_seed", 42))

        if len(daily) < 2:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={"n_days": float(len(daily))},
                details=f"insufficient sample for bootstrap: n={len(daily)}",
            )

        samples = day_bootstrap(daily, n_resamples=n_resamples, rng_seed=seed)
        means = samples.mean(axis=1)
        ci_lower = float(np.quantile(means, alpha))

        passed = ci_lower > ci_min
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "ci_lower": ci_lower,
                "ci_min": ci_min,
                "alpha": alpha,
                "n_resamples": float(n_resamples),
                "n_days": float(len(daily)),
            },
            details=(f"CI[{alpha:.2f}] lower={ci_lower:.4f} vs min {ci_min}"),
        )
