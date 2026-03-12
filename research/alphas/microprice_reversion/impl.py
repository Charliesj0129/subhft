"""Microprice Reversion Alpha.

Signal: -EMA_16((microprice_x2 - mid_price_x2) / max(spread_scaled, 1))
Mean-reversion on microprice-mid deviation, normalized by spread.

Allocator Law: __slots__ on class; all state is scalar.
Precision Law: output is float (signal score, not price).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 16 ticks -> alpha = 1 - exp(-1/16) ~ 0.0606
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 16.0)

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="microprice_reversion",
    hypothesis=(
        "Microprice-mid deviation predicts short-term mean-reversion:"
        " when microprice deviates above mid (bid side heavier),"
        " subsequent ticks tend to revert downward, and vice versa."
    ),
    formula="signal_t = -EMA_16((microprice_x2 - mid_price_x2) / max(spread_scaled, 1))",
    paper_refs=(),
    data_fields=("microprice_x2", "mid_price_x2", "spread_scaled"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class MicropriceReversionAlpha:
    """O(1) microprice-mid reversion predictor with EMA smoothing.

    update() accepts either:
      - 3 positional args:  microprice_x2, mid_price_x2, spread_scaled
      - keyword args:       microprice_x2=..., mid_price_x2=..., spread_scaled=...
    """

    __slots__ = ("_micro_dev_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._micro_dev_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Compute signal from microprice_x2, mid_price_x2, spread_scaled."""
        if len(args) >= 3:
            microprice_x2 = float(args[0])
            mid_price_x2 = float(args[1])
            spread_scaled = float(args[2])
        elif len(args) in (1, 2):
            raise ValueError(
                "update() requires 3 positional args "
                "(microprice_x2, mid_price_x2, spread_scaled) or keyword args"
            )
        else:
            microprice_x2 = float(kwargs.get("microprice_x2", 0.0))
            mid_price_x2 = float(kwargs.get("mid_price_x2", 0.0))
            spread_scaled = float(kwargs.get("spread_scaled", 0.0))

        # Normalize deviation by spread (max(spread, 1) guards zero-spread).
        denom = max(spread_scaled, 1.0)
        micro_dev = (microprice_x2 - mid_price_x2) / denom

        if not self._initialized:
            self._micro_dev_ema = micro_dev
            self._initialized = True
        else:
            self._micro_dev_ema += _EMA_ALPHA * (micro_dev - self._micro_dev_ema)

        # Negative sign: mean-reversion bet (fade the deviation).
        self._signal = -self._micro_dev_ema
        return self._signal

    def reset(self) -> None:
        self._micro_dev_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = MicropriceReversionAlpha

__all__ = ["MicropriceReversionAlpha", "ALPHA_CLASS"]
