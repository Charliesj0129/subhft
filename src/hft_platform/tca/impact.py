# src/hft_platform/tca/impact.py
"""Square-root impact model for TCA.

Model: impact_bps = eta * sigma * sqrt(V / ADV) * 10000
WARNING: float arithmetic — offline TCA analysis only.
"""

from __future__ import annotations

import math


class SqrtImpactModel:
    __slots__ = ("_sigma", "_adv", "_eta")

    def __init__(self, *, sigma_daily: float = 0.015, adv: int = 5000, eta: float = 0.1) -> None:
        self._sigma = sigma_daily
        self._adv = adv
        self._eta = eta

    def estimate_impact_bps(self, *, volume: int) -> float:
        if volume <= 0 or self._adv <= 0:
            return 0.0
        participation = volume / self._adv
        return self._eta * self._sigma * math.sqrt(participation) * 10_000.0
