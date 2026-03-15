"""Depth Replenishment Alpha — LOB depth recovery as directional signal.

Signal:  DR_t = EMA_8(Δtotal_depth) × sign(bid_qty - ask_qty)

Hypothesis: the speed at which LOB depth recovers after depletion indicates
market maker confidence.  Fast replenishment on one side signals expected
price move away from that side.

Δtotal_depth = (bid_qty + ask_qty) - prev(bid_qty + ask_qty)
sign(bid_qty - ask_qty) gives the side with more depth.
Positive DR → depth rebuilding on bid side → buy signal.
Negative DR → depth rebuilding on ask side → sell signal.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ≈ 8 ticks → α = 1 − exp(−1/8) ≈ 0.1175
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)
_EPSILON: float = 1e-8  # guards against division by zero

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="depth_replenishment",
    hypothesis=(
        "The speed at which LOB depth recovers after depletion indicates"
        " market maker confidence. Fast replenishment on one side signals"
        " expected price move away from that side."
    ),
    formula="DR_t = EMA_8(Δtotal_depth) × sign(bid_qty - ask_qty)",
    paper_refs=(),
    data_fields=("bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


def _sign(x: float) -> float:
    """Return -1.0, 0.0, or 1.0."""
    if x > _EPSILON:
        return 1.0
    if x < -_EPSILON:
        return -1.0
    return 0.0


class DepthReplenishmentAlpha:
    """O(1) depth-replenishment predictor with EMA smoothing.

    update() accepts either:
      - 2 positional args:  bid_qty, ask_qty
      - keyword args:       bid_qty=..., ask_qty=...
    """

    __slots__ = ("_prev_total_depth", "_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._prev_total_depth: float = 0.0
        self._ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Update signal with new bid_qty and ask_qty values."""
        # --- resolve bid_qty and ask_qty ---
        if len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif len(args) == 1:
            raise ValueError(
                "update() requires 2 positional args (bid_qty, ask_qty) or keyword args"
            )
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        total_depth = bid_qty + ask_qty

        if not self._initialized:
            # First tick: store prev, return 0 (no delta yet).
            self._prev_total_depth = total_depth
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # Compute depth delta and directional sign.
        delta_depth = total_depth - self._prev_total_depth
        self._prev_total_depth = total_depth

        side_sign = _sign(bid_qty - ask_qty)
        raw_dr = delta_depth * side_sign

        # EMA update.
        self._ema += _EMA_ALPHA * (raw_dr - self._ema)
        self._signal = self._ema
        return self._signal

    def reset(self) -> None:
        self._prev_total_depth = 0.0
        self._ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = DepthReplenishmentAlpha

__all__ = ["DepthReplenishmentAlpha", "ALPHA_CLASS"]
