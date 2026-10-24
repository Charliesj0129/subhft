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

from hft_platform.alpha._sub_gates.registry import SubGateResult, UnavailableDailyPnl
from hft_platform.alpha.outcomes import Outcome


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


def _required_days(n_fills: int, n_days: int, min_fills: int, min_days: int) -> int | None:
    """Trading days needed to clear both floors at the observed fill rate."""
    if n_days <= 0:
        return None
    needed = min_days
    if n_fills < min_fills:
        if n_fills <= 0:
            return None
        needed = max(needed, math.ceil(min_fills * n_days / n_fills))
    return max(needed, n_days + 1)


class MinSampleSizeGate:
    """Reject runs with too few fills or too few trading days.

    Targets the R47-OE1 fingerprint: 39 fills over 31 days.

    A short sample is INSUFFICIENT_SAMPLE, never FAIL. ``required_n`` is the
    trading days the observed fill rate needs to clear both floors; zero
    activity on an axis marks the sample unreachable. Taker profiles state the
    floor on the out-of-sample axis (``min_oos_trades`` / ``min_oos_days``);
    those floors are measured against ``n_oos_trades`` / ``n_oos_days`` only,
    and a result that does not report them is an ERROR.
    """

    name = "min_sample_size"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        oos_axis = (
            "min_fills" not in thresholds
            and "min_days" not in thresholds
            and ("min_oos_trades" in thresholds or "min_oos_days" in thresholds)
        )
        if oos_axis:
            min_fills = int(thresholds.get("min_oos_trades", 0))
            min_days = int(thresholds.get("min_oos_days", 0))
            raw_fills = getattr(result, "n_oos_trades", None)
            raw_days = getattr(result, "n_oos_days", None)
            if raw_fills is None or raw_days is None:
                return SubGateResult(
                    name=self.name,
                    passed=False,
                    outcome=Outcome.ERROR,
                    metrics={"axis": "oos", "min_fills": float(min_fills), "min_days": float(min_days)},
                    details="profile sets min_oos_trades/min_oos_days but the result reports no OOS sample counts",
                )
            n_fills, n_days = int(raw_fills), int(raw_days)
        else:
            min_fills = int(thresholds.get("min_fills", 0))
            min_days = int(thresholds.get("min_days", 0))
            if min_days > 0 and isinstance(getattr(result, "daily_pnl", None), UnavailableDailyPnl):
                return SubGateResult(
                    name=self.name,
                    passed=False,
                    outcome=Outcome.ERROR,
                    metrics={"axis": "full", "min_fills": float(min_fills), "min_days": float(min_days)},
                    details=f"trading-day count unavailable: {result.daily_pnl.reason}",
                )
            n_fills = int(getattr(result, "n_fills", 0) or 0)
            n_days = int(getattr(result, "n_trading_days", 0) or 0)

        passed = n_fills >= min_fills and n_days >= min_days

        fills_frac = _frac(n_fills, min_fills)
        days_frac = _frac(n_days, min_days)
        label = _label_for(fills_frac, days_frac)

        metrics: dict[str, Any] = {
            "axis": "oos" if oos_axis else "full",
            "n_fills": float(n_fills),
            "n_days": float(n_days),
            "min_fills": float(min_fills),
            "min_days": float(min_days),
            "fills_frac": fills_frac,
            "days_frac": days_frac,
            "sample_adequacy_label": label,
        }
        outcome = Outcome.PASS
        if not passed:
            outcome = Outcome.INSUFFICIENT_SAMPLE
            zero_activity = (min_fills > 0 and n_fills <= 0 and n_days > 0) or (min_days > 0 and n_days <= 0)
            metrics["required_n"] = None if zero_activity else _required_days(n_fills, n_days, min_fills, min_days)
            metrics["required_n_unit"] = "trading_days"
            metrics["reachable"] = False if (min_fills > 0 and n_fills <= 0 and n_days > 0) else None
        return SubGateResult(
            name=self.name,
            passed=passed,
            outcome=outcome,
            metrics=metrics,
            details=(f"fills={n_fills} (min {min_fills}), days={n_days} (min {min_days}), label={label}"),
        )
