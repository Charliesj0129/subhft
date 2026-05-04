"""Stationary block-bootstrap CI sub-gate."""

from __future__ import annotations

from typing import Any

import numpy as np

from hft_platform.alpha._resampling import stationary_block_bootstrap
from hft_platform.alpha._sub_gates.registry import SubGateResult


def _entry_to_float(entry: Any) -> float:
    if isinstance(entry, dict):
        return float(entry.get("pnl_pts", 0.0))
    return float(entry)


class StationaryBlockBootstrapGate:
    """Politis-Romano block-bootstrap mean lower CI bound > threshold."""

    name = "stationary_block_bootstrap"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        daily = [_entry_to_float(e) for e in (getattr(result, "daily_pnl", None) or [])]
        ci_min = float(thresholds.get("block_bootstrap_ci_lower_bound_min", 0.0))
        block_size = int(thresholds.get("block_bootstrap_block_size_days", 5))
        n_resamples = int(thresholds.get("block_bootstrap_n_resamples", 1000))
        alpha = float(thresholds.get("block_bootstrap_alpha", 0.05))
        seed = int(thresholds.get("block_bootstrap_rng_seed", 42))

        if len(daily) < block_size:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={"n_days": float(len(daily)), "block_size": float(block_size)},
                details=f"input length {len(daily)} < block_size {block_size}",
            )

        samples = stationary_block_bootstrap(daily, block_size=block_size, n_resamples=n_resamples, rng_seed=seed)
        means = samples.mean(axis=1)
        ci_lower = float(np.quantile(means, alpha))
        passed = ci_lower > ci_min

        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "ci_lower": ci_lower,
                "ci_min": ci_min,
                "alpha": alpha,
                "block_size": float(block_size),
                "n_resamples": float(n_resamples),
                "n_days": float(len(daily)),
            },
            details=(f"block-bootstrap CI[{alpha:.2f}] lower={ci_lower:.4f} (block={block_size}, n={n_resamples})"),
        )
