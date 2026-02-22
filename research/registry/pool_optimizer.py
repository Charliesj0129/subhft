from __future__ import annotations

from typing import Any, Mapping, Sequence

from hft_platform.alpha.pool import marginal_contribution_test, optimize_pool_weights


class AlphaPoolOptimizer:
    def optimize(
        self,
        *,
        signals: Mapping[str, Sequence[float]],
        method: str = "equal_weight",
        ridge_alpha: float = 0.1,
        returns: Sequence[float] | None = None,
    ) -> dict[str, float]:
        result = optimize_pool_weights(
            signals=signals,
            returns=returns,
            method=method,
            ridge_alpha=ridge_alpha,
        )
        return dict(result.weights)

    def marginal_test(
        self,
        *,
        new_signal: Sequence[float],
        existing_signals: Mapping[str, Sequence[float]],
        method: str = "equal_weight",
        min_uplift: float = 0.05,
        ridge_alpha: float = 0.1,
        returns: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        return marginal_contribution_test(
            new_signal=new_signal,
            existing_signals=existing_signals,
            method=method,
            min_uplift=min_uplift,
            ridge_alpha=ridge_alpha,
            returns=returns,
        )
