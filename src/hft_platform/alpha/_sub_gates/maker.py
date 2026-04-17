"""Maker-specific sub-gates."""
from __future__ import annotations

from typing import Any

from hft_platform.alpha._sub_gates.registry import SubGateResult


class FillQualityGate:
    """Check pnl_per_fill and adverse_fill_pct thresholds."""

    name = "fill_quality"
    applies_to = {"maker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        pnl_per_fill = result.pnl_per_fill or 0.0
        adverse_pct = (result.adverse_fill_pct or 0.0) * 100.0
        min_ppf = thresholds.get("pnl_per_fill_min_pts", 0.0)
        max_adverse = thresholds.get("adverse_fill_pct_max", 50.0)

        passed = pnl_per_fill >= min_ppf and adverse_pct <= max_adverse
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "pnl_per_fill": float(pnl_per_fill),
                "adverse_fill_pct": float(adverse_pct),
                "min_pnl_per_fill": float(min_ppf),
                "max_adverse_fill_pct": float(max_adverse),
            },
            details=(
                f"pnl/fill={pnl_per_fill:.2f} (min {min_ppf}), "
                f"adverse={adverse_pct:.1f}% (max {max_adverse})"
            ),
        )


class FillRateValidationGate:
    """Check backtest fill rate consistency with calibration profile.

    Large deviation indicates market regime change — backtest may be unreliable.
    Requires a calibration profile with `expected_fill_rate_per_day > 0`.
    """

    name = "fill_rate_validation"
    applies_to = {"maker"}

    def evaluate(
        self,
        result: Any,
        config: Any,
        thresholds: dict,
        profile: Any = None,
    ) -> SubGateResult:
        if profile is None or result.fill_rate_per_day is None:
            return SubGateResult(
                name=self.name,
                passed=True,
                metrics={},
                details="no calibration baseline — skipped",
            )
        expected = profile.expected_fill_rate_per_day
        actual = result.fill_rate_per_day
        if expected <= 0:
            return SubGateResult(
                name=self.name,
                passed=True,
                metrics={},
                details="baseline fill rate is zero — skipped",
            )

        deviation = abs(actual - expected) / expected
        max_dev = thresholds.get("fill_rate_deviation_max", 0.5)
        passed = deviation < max_dev
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "actual_fill_rate": float(actual),
                "expected_fill_rate": float(expected),
                "deviation": float(deviation),
                "max_deviation": float(max_dev),
            },
            details=(
                f"fill_rate={actual:.2f}/day vs expected {expected:.2f} "
                f"(dev={deviation*100:.1f}% vs max {max_dev*100:.0f}%)"
            ),
        )
