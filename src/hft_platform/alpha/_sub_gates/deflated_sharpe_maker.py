"""Deflated-Sharpe sub-gate for maker payloads.

Reuses the Bonferroni-style penalty from `_param_opt.py`:
    deflated_sharpe = sharpe_oos - sqrt(2 * log(n_trials) / oos_len)
"""

from __future__ import annotations

import math
from statistics import mean, stdev
from typing import Any

from hft_platform.alpha._sub_gates.registry import SubGateResult


def _entry_to_float(entry: Any) -> float:
    if isinstance(entry, dict):
        return float(entry.get("pnl_pts", 0.0))
    return float(entry)


class DeflatedSharpeForMakerGate:
    """Maker-side deflated Sharpe must exceed `deflated_sharpe_min`."""

    name = "deflated_sharpe_maker"
    applies_to = {"maker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        daily = [_entry_to_float(e) for e in (getattr(result, "daily_pnl", None) or [])]
        if len(daily) < 2:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={"n_days": float(len(daily))},
                details=f"insufficient days for Sharpe: n={len(daily)}",
            )

        m = mean(daily)
        s = stdev(daily)
        sharpe = (m / s) * math.sqrt(252.0) if s > 0 else 0.0
        n_trials = max(1, int(thresholds.get("deflated_n_trials", 1)))
        oos_len = max(2, len(daily))
        penalty = math.sqrt(2.0 * math.log(n_trials) / oos_len) if n_trials > 1 else 0.0
        deflated = sharpe - penalty

        threshold = float(thresholds.get("deflated_sharpe_min", 0.5))
        passed = deflated >= threshold

        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "sharpe": float(sharpe),
                "deflated_sharpe": float(deflated),
                "deflated_min": float(threshold),
                "n_trials": float(n_trials),
                "n_days": float(oos_len),
                "penalty": float(penalty),
            },
            details=(
                f"sharpe={sharpe:.2f}, deflated={deflated:.2f} "
                f"(penalty={penalty:.2f}, trials={n_trials}) vs min {threshold}"
            ),
        )
