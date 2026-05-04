"""Minimum-sample-size sub-gate (fills + trading days)."""
from __future__ import annotations

from typing import Any

from hft_platform.alpha._sub_gates.registry import SubGateResult


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
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "n_fills": float(n_fills),
                "n_days": float(n_days),
                "min_fills": float(min_fills),
                "min_days": float(min_days),
            },
            details=(
                f"fills={n_fills} (min {min_fills}), days={n_days} (min {min_days})"
            ),
        )
