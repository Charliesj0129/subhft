"""Selection-deflated Sharpe: the daily Sharpe less what the best of the search is expected to reach.

An alpha that a search picked as the best of many expressions carries that
search's selection bias into Gate C. Gate C passes the search's effective
trial count (``experiment_metadata.search_trials_effective``, the Li-Ji count
``AlphaSearchEngine.trial_summary`` records) in as ``search_trials_effective``.
The annualized daily Sharpe less ``stats.selection_sr0`` for that count is
compared with ``deflated_sharpe_min``; the Bailey/Lopez de Prado probability
is reported beside it.

The effective count, not the raw distinct count, is the deflator: near
duplicate expressions are one test. When the alpha has no search record the
gate is NOT_APPLICABLE. It blocks only when a profile lists it in
``blocking_sub_gates``; no profile does yet, so it reports.
"""

from __future__ import annotations

import math
from statistics import mean, stdev
from typing import Any

from hft_platform.alpha._sub_gates.common import _to_daily_series
from hft_platform.alpha._sub_gates.registry import SubGateResult
from hft_platform.alpha.outcomes import Outcome
from hft_platform.alpha.stats import deflated_sharpe, null_sharpe_std, sample_skew_kurtosis, selection_sr0

_TRADING_DAYS_PER_YEAR = 252.0


class SelectionDeflatedSharpeGate:
    """Daily Sharpe less the search's expected-max Sharpe must reach `deflated_sharpe_min`."""

    name = "selection_deflated_sharpe"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        raw_trials = thresholds.get("search_trials")
        effective = thresholds.get("search_trials_effective")
        if not effective:
            details = (
                f"search recorded {int(raw_trials)} distinct trials but no effective count"
                if raw_trials
                else "no search record: the alpha was not selected by a recorded search"
            )
            return SubGateResult(name=self.name, passed=False, details=details, outcome=Outcome.NOT_APPLICABLE)

        n_trials = max(1, int(effective))
        daily = _to_daily_series(getattr(result, "daily_pnl", None))
        if len(daily) < 2:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={"n_days": float(len(daily)), "n_trials_effective": float(n_trials)},
                details=f"insufficient days for Sharpe: n={len(daily)}",
                outcome=Outcome.INSUFFICIENT_SAMPLE,
            )

        n_days = len(daily)
        s = stdev(daily)
        daily_sharpe = mean(daily) / s if s > 0 else 0.0
        sharpe = daily_sharpe * math.sqrt(_TRADING_DAYS_PER_YEAR)
        penalty = selection_sr0(n_trials, observations=n_days, annualization=_TRADING_DAYS_PER_YEAR)
        deflated = sharpe - penalty
        skewness, kurtosis = sample_skew_kurtosis(daily)
        probability = deflated_sharpe(
            daily_sharpe,
            effective_trials=n_trials,
            observations=n_days,
            trial_sharpe_std=null_sharpe_std(n_days),
            skewness=skewness,
            kurtosis=kurtosis,
        )
        threshold = float(thresholds.get("deflated_sharpe_min", 0.5))
        metrics: dict[str, Any] = {
            "sharpe": float(sharpe),
            "deflated_sharpe": float(deflated),
            "deflated_min": threshold,
            "n_trials_effective": float(n_trials),
            "n_days": float(n_days),
            "penalty": float(penalty),
            "dsr_probability": float(probability),
        }
        if raw_trials:
            metrics["n_trials_distinct"] = float(raw_trials)
        return SubGateResult(
            name=self.name,
            passed=deflated >= threshold,
            metrics=metrics,
            details=(
                f"sharpe={sharpe:.2f}, deflated={deflated:.2f} "
                f"(SR0={penalty:.2f}, effective trials={n_trials}, DSR p={probability:.3f}) vs min {threshold}"
            ),
        )
