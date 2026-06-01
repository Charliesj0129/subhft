"""Minimum-sample-size sub-gate (fills + trading days).

Beyond the binary pass/fail, this gate also emits a
``sample_adequacy_label`` (goal §4) so sub-threshold runs route to the
right triage bucket instead of being indistinguishable failures:

    adequate            : both fractions >= 1.0 — gate passes
    promising           : min fraction in [0.5, 1.0) — close, re-run later
    needs_more_sample   : min fraction in (0, 0.5)
    inconclusive        : zero activity on at least one axis

The label is informational. ``passed`` still flips only when both fills
and days clear their thresholds — goal 限制 §3 forbids relaxing the
sample bar.
"""

from __future__ import annotations

import math
from typing import Any

from hft_platform.alpha._sub_gates.registry import SubGateResult


def _frac(n: int, minimum: int) -> float:
    """Return n / minimum, with minimum<=0 treated as no-threshold (inf)."""
    if minimum <= 0:
        return math.inf
    return n / minimum


def _label_for(fills_frac: float, days_frac: float) -> str:
    min_frac = min(fills_frac, days_frac)
    if min_frac >= 1.0:
        return "adequate"
    if min_frac == 0.0:
        return "inconclusive"
    if min_frac >= 0.5:
        return "promising"
    return "needs_more_sample"


class MinSampleSizeGate:
    """Reject runs with too few fills or too few trading days.

    Targets the R47-OE1 fingerprint: 39 fills over 31 days.
    """

    name = "min_sample_size"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        n_fills = int(getattr(result, "n_fills", 0) or 0)
        n_days = int(getattr(result, "n_trading_days", 0) or 0)
        min_fills = int(thresholds.get("min_fills", 0))
        min_days = int(thresholds.get("min_days", 0))

        passed = n_fills >= min_fills and n_days >= min_days

        fills_frac = _frac(n_fills, min_fills)
        days_frac = _frac(n_days, min_days)
        label = _label_for(fills_frac, days_frac)

        metrics: dict[str, Any] = {
            "n_fills": float(n_fills),
            "n_days": float(n_days),
            "min_fills": float(min_fills),
            "min_days": float(min_days),
            "fills_frac": fills_frac,
            "days_frac": days_frac,
            "sample_adequacy_label": label,
        }
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics=metrics,
            details=(f"fills={n_fills} (min {min_fills}), days={n_days} (min {min_days}), label={label}"),
        )
