"""Deflated-Sharpe sub-gate for maker payloads.

The annualized daily Sharpe less ``SR0``, the Sharpe the best of
``deflated_n_trials`` null trials is expected to reach
(``stats.selection_sr0``), must reach ``deflated_sharpe_min``. The trials
were not kept, so their spread is the null estimator spread over the same
days; one trial deflates nothing. The Bailey/Lopez de
Prado probability is reported beside it as ``dsr_probability``.
"""

from __future__ import annotations

import math
from statistics import mean, stdev
from typing import Any

from hft_platform.alpha._sub_gates.common import _to_daily_series
from hft_platform.alpha._sub_gates.registry import SubGateResult
from hft_platform.alpha.stats import deflated_sharpe, null_sharpe_std, sample_skew_kurtosis, selection_sr0

_TRADING_DAYS_PER_YEAR = 252.0


class DeflatedSharpeForMakerGate:
    """Maker-side deflated Sharpe must exceed `deflated_sharpe_min`."""

    name = "deflated_sharpe_maker"
    applies_to = {"maker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        daily = _to_daily_series(getattr(result, "daily_pnl", None))
        if len(daily) < 2:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={"n_days": float(len(daily))},
                details=f"insufficient days for Sharpe: n={len(daily)}",
            )

        m = mean(daily)
        s = stdev(daily)
        daily_sharpe = m / s if s > 0 else 0.0
        sharpe = daily_sharpe * math.sqrt(_TRADING_DAYS_PER_YEAR)
        n_trials = max(1, int(thresholds.get("deflated_n_trials", 1)))
        n_days = len(daily)
        trial_std = null_sharpe_std(n_days)
        penalty = selection_sr0(n_trials, observations=n_days, annualization=_TRADING_DAYS_PER_YEAR)
        deflated = sharpe - penalty
        skewness, kurtosis = sample_skew_kurtosis(daily)
        probability = deflated_sharpe(
            daily_sharpe,
            effective_trials=n_trials,
            observations=n_days,
            trial_sharpe_std=trial_std,
            skewness=skewness,
            kurtosis=kurtosis,
        )

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
                "n_days": float(n_days),
                "penalty": float(penalty),
                "dsr_probability": float(probability),
            },
            details=(
                f"sharpe={sharpe:.2f}, deflated={deflated:.2f} "
                f"(SR0={penalty:.2f}, trials={n_trials}, DSR p={probability:.3f}) vs min {threshold}"
            ),
        )
