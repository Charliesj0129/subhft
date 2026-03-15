"""Amihud Illiquidity — |return|/volume ratio (Amihud 2002).

Measures price impact per unit of trading volume. High values indicate
information-driven trading and low liquidity.

Laws: Allocator (slots, no alloc), Cache (scalar state), Precision (no float prices).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

_EMA_ALPHA_16: float = 1.0 - math.exp(-1.0 / 16.0)
_EPSILON: float = 1e-8

_MANIFEST = AlphaManifest(
    alpha_id="amihud_illiquidity",
    hypothesis=(
        "Amihud illiquidity ratio |return|/volume measures price impact"
        " per unit volume, indicating information-driven trading"
    ),
    formula="AI_t = EMA_16(|ΔP/P| / max(volume, ε))",
    paper_refs=(),
    data_fields=("mid_price", "volume"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class AmihudIlliquidityAlpha:
    """O(1) Amihud illiquidity ratio with EMA-16 smoothing.

    update() accepts keyword args: mid_price=..., volume=...
    Signal is always non-negative (absolute return / volume).
    """

    __slots__ = ("_prev_mid", "_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._prev_mid: float = 0.0
        self._ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: ANN002
        mid_price = float(kwargs.get("mid_price", 0.0))
        volume = float(kwargs.get("volume", 0.0))

        if not self._initialized:
            self._prev_mid = mid_price
            self._ema = 0.0
            self._initialized = True
            self._signal = 0.0
            return self._signal

        abs_ret = abs(mid_price - self._prev_mid) / max(self._prev_mid, _EPSILON)
        illiq = abs_ret / max(volume, _EPSILON)

        self._ema += _EMA_ALPHA_16 * (illiq - self._ema)
        self._signal = self._ema
        self._prev_mid = mid_price
        return self._signal

    def reset(self) -> None:
        self._prev_mid = 0.0
        self._ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = AmihudIlliquidityAlpha
__all__ = ["AmihudIlliquidityAlpha", "ALPHA_CLASS"]
