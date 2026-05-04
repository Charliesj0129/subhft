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
                metrics={"threshold": threshold},
                details="replay_parity_report missing on result; cannot certify parity",
            )

        match_pct = float(getattr(report, "match_pct", 0.0))
        # `or -1` guards against first_divergence_idx=None; explicit cast
        # keeps the metrics dict json-serializable.
        first_div = float(getattr(report, "first_divergence_idx", -1) or -1)

        passed = match_pct >= threshold
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "match_pct": match_pct,
                "threshold": threshold,
                "first_divergence_idx": first_div,
            },
            details=(
                f"match_pct={match_pct:.2f}% vs min {threshold:.2f}% "
                f"(first_divergence_idx={first_div:.0f})"
            ),
        )
