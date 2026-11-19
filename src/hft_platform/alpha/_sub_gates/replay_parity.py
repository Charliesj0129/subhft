"""Replay parity sub-gate.

Validates that a strategy's deterministic replay of a recorded intent log
matches the original within a configurable tolerance. This is a pure
sub-gate: it consumes a precomputed ``replay_parity_report`` attached to
the backtest result and applies the threshold check. No I/O, no state.

Slice C of the replay-parity gate hardening; see
``docs/superpowers/plans/2026-05-04-slice-c-replay-parity-gate.md``.
"""

from __future__ import annotations

from typing import Any

from hft_platform.alpha._sub_gates.registry import SubGateResult
from hft_platform.alpha.divergence_category import categorize_histogram
from hft_platform.alpha.outcomes import Outcome


class ReplayParityGate:
    """Check replay-parity match percentage against threshold.

    Expects ``result.replay_parity_report`` to expose:
      * ``match_pct: float`` — percentage of intents that matched on replay.
      * ``first_divergence_idx: int | None`` — index of first mismatch
        (None when there is no divergence).

    A missing report (None) is a hard failure: the gate cannot certify
    parity it never observed.
    """

    name = "replay_parity"
    applies_to = {"maker", "taker"}

    expected_parity_dimensions = ("session_phase",)

    def evaluate(
        self,
        result: Any,
        config: Any,
        thresholds: dict,
    ) -> SubGateResult:
        threshold = float(thresholds.get("replay_parity_match_pct_min", 95.0))

        report = getattr(result, "replay_parity_report", None)
        if report is None:
            return SubGateResult(
                name=self.name,
                passed=False,
                outcome=Outcome.NOT_APPLICABLE,
                metrics={"threshold": threshold},
                details="replay_parity_report missing on result; no live intents to compare (ADR-003)",
            )

        match_pct = float(getattr(report, "match_pct", 0.0))
        first_div = float(getattr(report, "first_divergence_idx", -1) or -1)
        histogram = getattr(report, "divergence_histogram", None) or {}
        category_counts = categorize_histogram(histogram)
        dominant_category = max(category_counts, key=lambda k: category_counts[k]) if category_counts else ""

        observed_fields = set(getattr(report, "observed_fields", ()) or ())
        uncovered = [f for f in self.expected_parity_dimensions if f not in observed_fields]

        passed = match_pct >= threshold
        metrics: dict[str, Any] = {
            "match_pct": match_pct,
            "threshold": threshold,
            "first_divergence_idx": first_div,
            "divergence_categories": category_counts,
            "dominant_divergence_category": dominant_category,
            "per_field_divergences": dict(histogram),
            "uncovered_parity_dimensions": uncovered,
        }
        suffix = f", dominant_category={dominant_category}" if dominant_category else ""
        if uncovered:
            suffix += f", uncovered_parity_dimensions={'/'.join(uncovered)}"
        return SubGateResult(
            name=self.name,
            passed=passed,
            outcome=Outcome.PASS if passed else Outcome.ERROR,
            metrics=metrics,
            details=(
                f"match_pct={match_pct:.2f}% vs min {threshold:.2f}% (first_divergence_idx={first_div:.0f}{suffix})"
            ),
        )
