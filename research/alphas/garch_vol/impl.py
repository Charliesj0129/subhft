"""GARCH Vol Alpha — short/long volatility ratio as regime indicator.

Signal:  GV_t = EMA_8(dP^2) / EMA_64(dP^2) - 1
         where dP = mid_price_t - mid_price_{t-1}

Hypothesis: Volatility clusters (GARCH effect) — high volatility predicts
continued high volatility.  The ratio of short-term to long-term volatility
indicates regime (trending vs mean-reverting).  Positive = vol expansion,
negative = vol contraction.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay constants
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)    # window ~ 8 ticks
_EMA_ALPHA_64: float = 1.0 - math.exp(-1.0 / 64.0)   # window ~ 64 ticks
_EPSILON: float = 1e-8  # guards against division by zero

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="garch_vol",
    hypothesis=(
        "Volatility clusters (GARCH effect) — high volatility predicts"
        " continued high volatility.  The ratio of short-term to long-term"
        " volatility indicates regime (trending vs mean-reverting)."
    ),
    formula="GV_t = EMA_8(dP^2) / EMA_64(dP^2) - 1",
    paper_refs=("021",),
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


class GARCHVolAlpha:
    """O(1) GARCH-style volatility regime detector with dual EMA.

    update() accepts either:
      - 1 positional arg:  mid_price
      - keyword arg:       mid_price=...
    """

    __slots__ = (
        "_prev_mid",
        "_ema_short",
        "_ema_long",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        self._prev_mid: float = 0.0
        self._ema_short: float = 0.0
        self._ema_long: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: ANN002
        """Update signal with new mid_price value."""
        # --- resolve mid_price ---
        if len(args) >= 1:
            mid_price = float(args[0])
        else:
            mid_price = float(kwargs.get("mid_price", 0.0))

        if not self._initialized:
            # First tick: store prev, return 0 (no delta yet).
            self._prev_mid = mid_price
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # Compute squared return.
        delta = mid_price - self._prev_mid
        self._prev_mid = mid_price
        delta_sq = delta * delta

        # Dual EMA update on squared returns.
        self._ema_short += _EMA_ALPHA_8 * (delta_sq - self._ema_short)
        self._ema_long += _EMA_ALPHA_64 * (delta_sq - self._ema_long)

        # Ratio - 1: positive = vol expansion, negative = vol contraction.
        self._signal = self._ema_short / (self._ema_long + _EPSILON) - 1.0
        return self._signal

    def reset(self) -> None:
        self._prev_mid = 0.0
        self._ema_short = 0.0
        self._ema_long = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = GARCHVolAlpha

__all__ = ["GARCHVolAlpha", "ALPHA_CLASS"]
