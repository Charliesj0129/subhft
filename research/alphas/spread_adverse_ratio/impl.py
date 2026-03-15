"""Spread Adverse Selection Fraction — ref 131 (Cartea & Sanchez-Betancourt 2025).

Decomposes the observed bid-ask spread into a volatility-driven component and
an adverse-selection residual.  The signal is the fraction of the spread that
is *not* explained by recent price volatility:

    delta_mid       = |mid_price - prev_mid|
    vol_proxy       = delta_mid * sqrt(volume)
    vol_proxy_ema   = EMA_16(vol_proxy)
    vol_component   = kappa * vol_proxy_ema
    adverse         = spread_scaled - vol_component
    signal          = clip(adverse / max(spread_scaled, 1), 0, 1)

Signal range [0, 1]:
    0 = spread fully explained by volatility (no adverse selection)
    1 = spread entirely due to adverse selection (toxic flow regime)

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 16 ticks -> alpha = 1 - exp(-1/16) ~ 0.0606
_EMA_ALPHA_16: float = 1.0 - math.exp(-1.0 / 16.0)
_KAPPA: float = 1.0  # Calibration constant for vol -> spread mapping

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="spread_adverse_ratio",
    hypothesis=(
        "The fraction of bid-ask spread attributable to adverse selection"
        " (after removing the volatility-driven component) measures the"
        " toxicity regime; high adverse selection ratio indicates informed"
        " traders are active and market makers widen spreads beyond what"
        " volatility alone justifies."
    ),
    formula=(
        "signal = clip((spread - kappa * EMA_16(|dMid| * sqrt(vol)))"
        " / max(spread, 1), 0, 1)"
    ),
    paper_refs=("131",),
    data_fields=("spread_scaled", "mid_price", "volume"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class SpreadAdverseRatioAlpha:
    """O(1) adverse-selection fraction with EMA-16 volatility proxy.

    update() accepts either:
      - 3 positional args:  spread_scaled, mid_price, volume
      - keyword args:       spread_scaled=..., mid_price=..., volume=...
    """

    __slots__ = ("_prev_mid", "_vol_proxy_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._prev_mid: float = 0.0
        self._vol_proxy_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        # --- resolve spread_scaled, mid_price, volume ---
        if len(args) == 3:
            spread_scaled = float(args[0])
            mid_price = float(args[1])
            volume = float(args[2])
        elif args:
            raise ValueError(
                "update() requires 3 positional args"
                " (spread_scaled, mid_price, volume) or keyword args"
            )
        else:
            spread_scaled = float(kwargs.get("spread_scaled", 0.0))
            mid_price = float(kwargs.get("mid_price", 0.0))
            volume = float(kwargs.get("volume", 0.0))

        # Step 1: delta_mid (0.0 on first tick)
        if not self._initialized:
            delta_mid = 0.0
            self._prev_mid = mid_price
            self._initialized = True
        else:
            delta_mid = abs(mid_price - self._prev_mid)
            self._prev_mid = mid_price

        # Step 2: vol_proxy
        vol_proxy = delta_mid * math.sqrt(max(volume, 0.0))

        # Step 3: EMA update
        self._vol_proxy_ema += _EMA_ALPHA_16 * (vol_proxy - self._vol_proxy_ema)

        # Step 4: vol_component
        vol_component = _KAPPA * self._vol_proxy_ema

        # Step 5: adverse_component
        adverse_component = spread_scaled - vol_component

        # Step 6: signal = clip(adverse / max(spread, 1), 0, 1)
        denom = max(spread_scaled, 1.0)
        raw_signal = adverse_component / denom
        self._signal = max(0.0, min(1.0, raw_signal))

        return self._signal

    def reset(self) -> None:
        self._prev_mid = 0.0
        self._vol_proxy_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = SpreadAdverseRatioAlpha

__all__ = ["SpreadAdverseRatioAlpha", "ALPHA_CLASS"]
