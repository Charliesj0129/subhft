from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier


@dataclass
class GARCHResult:
    variance_forecast: float
    vol_forecast: float
    shock_squared: float


class GARCHFactor:
    """Online GARCH(1,1) volatility estimator."""

    def __init__(
        self,
        omega: float = 1e-6,
        alpha: float = 0.09,
        beta: float = 0.90,
        initial_vol: float = 0.001,
    ):
        self.omega = omega
        self.alpha = alpha
        self.beta = beta
        self.initial_vol = initial_vol
        self.prev_sigma2 = initial_vol**2
        self.prev_return = 0.0
        self.last_price: float | None = None
        self._signal = initial_vol

    def reset(self) -> None:
        self.prev_sigma2 = self.initial_vol**2
        self.prev_return = 0.0
        self.last_price = None
        self._signal = self.initial_vol

    def update(self, price: float) -> Optional[GARCHResult]:
        if self.last_price is None:
            self.last_price = price
            return None

        if self.last_price > 0:
            ret = float(np.log(price / self.last_price))
        else:
            ret = 0.0

        self.last_price = price
        epsilon2 = ret**2
        new_sigma2 = self.omega + self.alpha * epsilon2 + self.beta * self.prev_sigma2
        self.prev_sigma2 = new_sigma2
        self.prev_return = ret
        vol = float(np.sqrt(new_sigma2))
        self._signal = vol
        return GARCHResult(
            variance_forecast=float(new_sigma2),
            vol_forecast=vol,
            shock_squared=float(epsilon2),
        )

    def get_current_forecast(self) -> float:
        return float(np.sqrt(self.prev_sigma2))

    def get_signal(self) -> float:
        return self._signal


class GARCHVolAlpha(GARCHFactor):
    @property
    def manifest(self) -> AlphaManifest:
        return AlphaManifest(
            alpha_id="garch_vol",
            hypothesis="Online GARCH forecast captures near-term conditional volatility regime.",
            formula="sigma^2_t = omega + alpha * r^2_{t-1} + beta * sigma^2_{t-1}",
            paper_refs=("021",),
            data_fields=("price",),
            complexity="O(1)",
            status=AlphaStatus.DRAFT,
            tier=AlphaTier.ENSEMBLE,
            rust_module=None,
        )

    def update(self, *args, **kwargs) -> float:
        if args:
            price = float(args[0])
        else:
            price = float(kwargs.get("price", kwargs.get("current_price", 0.0)))
        result = super().update(price=price)
        if result is None:
            return 0.0
        return result.vol_forecast


ALPHA_CLASS = GARCHVolAlpha

__all__ = ["GARCHResult", "GARCHFactor", "GARCHVolAlpha", "ALPHA_CLASS"]
