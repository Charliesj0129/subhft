"""Price Level Revert Alpha — mean-reversion on mid-price deviation from EMA-128.

Signal:  -EMA_16((mid_price_x2 - EMA_128(mid_price_x2)) / max(spread_scaled, 1))

Hypothesis:
  Price deviation from a longer-term moving average (128-tick EMA), normalized
  by the current spread, is mean-reverting. The negative sign fades the deviation:
  when price is above the slow EMA the signal is negative (sell bias), when below
  it is positive (buy bias).

Different from microprice_reversion which uses the microprice-mid gap.

Allocator Law  : __slots__ on class; all state is scalar float/bool.
Precision Law  : inputs are scaled int (mid_price_x2, spread_scaled); signal is
                 a float score (not a price — no Decimal needed).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay coefficients
_EMA_ALPHA_16: float = 1.0 - math.exp(-1.0 / 16.0)
_EMA_ALPHA_128: float = 1.0 - math.exp(-1.0 / 128.0)

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="price_level_revert",
    hypothesis=(
        "Price deviation from a 128-tick EMA of mid_price_x2, normalized by spread, "
        "is mean-reverting. Fading the deviation captures short-horizon reversion "
        "that differs from microprice-based signals."
    ),
    formula="-EMA_16((mid_price_x2 - EMA_128(mid_price_x2)) / max(spread_scaled, 1))",
    paper_refs=(),
    data_fields=("mid_price_x2", "spread_scaled"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class PriceLevelRevertAlpha:
    """O(1) mean-reversion alpha on mid-price deviation from slow EMA.

    State (4 scalar slots, pre-allocated):
      _mid_ema128  : slow EMA of mid_price_x2 (128-tick half-life)
      _dev_ema16   : fast EMA of normalized deviation (16-tick half-life)
      _signal      : cached output (negated _dev_ema16)
      _initialized : whether the first tick has been seen

    update() accepts either:
      - 2 positional args:  mid_price_x2, spread_scaled
      - keyword args:       mid_price_x2=..., spread_scaled=...
    """

    __slots__ = ("_mid_ema128", "_dev_ema16", "_signal", "_initialized")

    def __init__(self) -> None:
        self._mid_ema128: float = 0.0
        self._dev_ema16: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Update state with new tick data and return the current signal.

        Parameters
        ----------
        mid_price_x2 : int | float
            Twice the mid price (scaled integer from LOBStatsEvent).
        spread_scaled : int | float
            Spread in scaled integer units (from LOBStatsEvent).
        """
        if len(args) >= 2:
            mid_price_x2 = float(args[0])
            spread_scaled = float(args[1])
        elif "mid_price_x2" in kwargs and "spread_scaled" in kwargs:
            mid_price_x2 = float(kwargs["mid_price_x2"])
            spread_scaled = float(kwargs["spread_scaled"])
        else:
            mid_price_x2 = float(kwargs.get("mid_price_x2", 0.0))
            spread_scaled = float(kwargs.get("spread_scaled", 0.0))

        # Update slow EMA of mid_price_x2
        if not self._initialized:
            self._mid_ema128 = mid_price_x2
            self._initialized = True
        else:
            self._mid_ema128 += _EMA_ALPHA_128 * (mid_price_x2 - self._mid_ema128)

        # Compute deviation normalized by spread
        deviation = mid_price_x2 - self._mid_ema128
        norm_spread = max(spread_scaled, 1.0)
        normalized_dev = deviation / norm_spread

        # Update fast EMA of normalized deviation
        self._dev_ema16 += _EMA_ALPHA_16 * (normalized_dev - self._dev_ema16)

        # Negate: fade the deviation (mean-reversion)
        self._signal = -self._dev_ema16
        return self._signal

    def reset(self) -> None:
        """Clear all EMA state to zero."""
        self._mid_ema128 = 0.0
        self._dev_ema16 = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = PriceLevelRevertAlpha

__all__ = ["PriceLevelRevertAlpha", "ALPHA_CLASS"]
