"""Taker-specific sub-gates.

Currently provides ICEvaluationGate. Additional taker-specific gates
(trend_contamination, oos_statistical) are deferred to a post-Plan-C
task, since they already exist as `_evaluate_*` helpers in _gate_c.py
and will be migrated in-place during the Gate C rewrite.
"""
from __future__ import annotations

from typing import Any

from hft_platform.alpha._sub_gates.registry import SubGateResult


class ICEvaluationGate:
    """IC (information coefficient) threshold check for taker strategies.

    Requires both in-sample (ic_is) and out-of-sample (ic_oos) ICs to
    exceed their respective thresholds. Returns pass=False if either
    is missing (None).
    """

    name = "ic_evaluation"
    applies_to = {"taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        ic_is = result.ic_is
        ic_oos = result.ic_oos
        if ic_is is None or ic_oos is None:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={},
                details="ic_is or ic_oos not computed",
            )
        min_is = thresholds.get("ic_is_min", 0.03)
        min_oos = thresholds.get("ic_oos_min", 0.02)
        passed = ic_is >= min_is and ic_oos >= min_oos
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "ic_is": float(ic_is),
                "ic_oos": float(ic_oos),
                "is_threshold": float(min_is),
                "oos_threshold": float(min_oos),
            },
            details=f"IC: IS={ic_is:.3f} OOS={ic_oos:.3f}",
        )
