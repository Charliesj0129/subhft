"""Momentum Decay Alpha — fast/slow EMA ratio of price changes.

Signal:  MD_t = EMA_4(DP) / max(|EMA_32(DP)|, eps) - sign(EMA_32(DP))

Hypothesis: short-term momentum decays at a measurable rate.  When the
fast EMA of price changes dominates the slow EMA, momentum is fresh and
likely to continue.  When it fades, expect reversal.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay constants: alpha = 1 - exp(-1/window)
_EMA_ALPHA_4: float = 1.0 - math.exp(-1.0 / 4.0)
_EMA_ALPHA_32: float = 1.0 - math.exp(-1.0 / 32.0)

_EPSILON: float = 1e-8

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="momentum_decay",
    hypothesis=(
        "Short-term momentum decays at a measurable rate. The ratio of"
        " fast EMA to slow EMA of price changes reveals whether momentum"
        " is building or fading. When fast/slow ratio is high, momentum"
        " is fresh; when it drops, expect reversal."
    ),
    formula="MD_t = EMA_4(DP) / max(|EMA_32(DP)|, eps) - sign(EMA_32(DP))",
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


class MomentumDecayAlpha:
    """O(1) momentum-decay predictor with dual-EMA smoothing.

    update() accepts either:
      - 1 positional arg:  mid_price
      - keyword arg:       mid_price=...
    """

    __slots__ = ("_prev_mid", "_fast_ema", "_slow_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._prev_mid: float = 0.0
        self._fast_ema: float = 0.0
        self._slow_ema: float = 0.0
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

        # Compute price change.
        delta_p = mid_price - self._prev_mid
        self._prev_mid = mid_price

        # Update fast and slow EMAs of price changes.
        self._fast_ema += _EMA_ALPHA_4 * (delta_p - self._fast_ema)
        self._slow_ema += _EMA_ALPHA_32 * (delta_p - self._slow_ema)

        # Momentum decay signal: ratio minus sign of slow trend.
        slow_abs = abs(self._slow_ema)
        denom = max(slow_abs, _EPSILON)
        ratio = self._fast_ema / denom

        if self._slow_ema > _EPSILON:
            sign_slow = 1.0
        elif self._slow_ema < -_EPSILON:
            sign_slow = -1.0
        else:
            sign_slow = 0.0

        self._signal = ratio - sign_slow
        return self._signal

    def reset(self) -> None:
        self._prev_mid = 0.0
        self._fast_ema = 0.0
        self._slow_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = MomentumDecayAlpha

__all__ = ["MomentumDecayAlpha", "ALPHA_CLASS"]
