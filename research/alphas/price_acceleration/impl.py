"""Price Acceleration Alpha — second derivative of mid-price, EMA-smoothed.

Signal:  PA_t = EMA_8(ΔP_t - ΔP_{t-1})
         where ΔP_t = mid_price_t - mid_price_{t-1}

Hypothesis: Price acceleration (momentum-of-momentum) captures regime shifts.
Positive acceleration = momentum building, negative = momentum fading/reversal.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
                 mid_price is a scaled int from LOBStatsEvent.
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="price_acceleration",
    hypothesis=(
        "Price acceleration (second derivative of mid-price) captures"
        " momentum-of-momentum: positive acceleration signals momentum"
        " building, negative signals momentum fading or reversal."
    ),
    formula="PA_t = EMA_8(delta_t - delta_{t-1})",
    paper_refs=(),
    data_fields=("mid_price",),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class PriceAccelerationAlpha:
    """O(1) price acceleration predictor with EMA smoothing.

    Computes the second difference of mid_price (acceleration) and smooths
    it with an EMA(8).  Needs 3 ticks before producing a meaningful signal:
      - tick 0: stores first mid_price
      - tick 1: computes first delta, stores it
      - tick 2+: computes acceleration = delta - prev_delta, EMA-smooths it

    update() accepts either:
      - 1 positional arg:  mid_price
      - keyword arg:       mid_price=...
    """

    __slots__ = ("_prev_mid", "_prev_delta", "_accel_ema", "_signal", "_tick_count")

    def __init__(self) -> None:
        self._prev_mid: float = 0.0
        self._prev_delta: float = 0.0
        self._accel_ema: float = 0.0
        self._signal: float = 0.0
        self._tick_count: int = 0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: ANN002
        """Update signal with a new mid_price value."""
        if len(args) >= 1:
            mid_price = float(args[0])
        else:
            mid_price = float(kwargs.get("mid_price", 0.0))

        if self._tick_count == 0:
            # First tick: store mid_price, no delta yet.
            self._prev_mid = mid_price
            self._tick_count = 1
            self._signal = 0.0
            return self._signal

        # Compute first difference (delta).
        delta = mid_price - self._prev_mid
        self._prev_mid = mid_price

        if self._tick_count == 1:
            # Second tick: have first delta but no acceleration yet.
            self._prev_delta = delta
            self._tick_count = 2
            self._signal = 0.0
            return self._signal

        # Third tick onward: compute acceleration (second difference).
        accel = delta - self._prev_delta
        self._prev_delta = delta

        # EMA update on acceleration.
        self._accel_ema += _EMA_ALPHA_8 * (accel - self._accel_ema)
        self._signal = self._accel_ema

        return self._signal

    def reset(self) -> None:
        self._prev_mid = 0.0
        self._prev_delta = 0.0
        self._accel_ema = 0.0
        self._signal = 0.0
        self._tick_count = 0

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = PriceAccelerationAlpha

__all__ = ["PriceAccelerationAlpha", "ALPHA_CLASS"]
