"""Square-root market impact model based on 2506.07711v5."""
from __future__ import annotations

import math


class SqrtImpactModel:
    """Estimate market impact using the square-root law.

    Impact ≈ σ × sqrt(Q / V) × η

    Where:
        σ (volatility): price volatility in bps
        Q (qty): order quantity in contracts
        V (avg_volume): average daily volume in contracts
        η (eta): scaling constant (default 1.0, calibrated per symbol)

    Result is in bps.
    """

    __slots__ = ("_eta",)

    def __init__(self, eta: float = 1.0) -> None:
        self._eta = eta

    def estimate(self, qty: int, volatility: float, avg_volume: float) -> float:
        """Estimate market impact in bps.

        Args:
            qty: Order quantity in contracts.
            volatility: Price volatility in bps.
            avg_volume: Average daily volume in contracts. Must be > 0.

        Returns:
            Estimated market impact in bps. Returns 0.0 if avg_volume <= 0.
        """
        if avg_volume <= 0:
            return 0.0
        return volatility * math.sqrt(qty / avg_volume) * self._eta
