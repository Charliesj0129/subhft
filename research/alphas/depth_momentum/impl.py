"""Depth Momentum Alpha.

Signal: EMA_8(delta(depth_imb))
First derivative of depth imbalance — captures rate of change of LOB asymmetry.

Allocator Law: __slots__ on class; all state is scalar.
Precision Law: output is float (signal score, not price).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)
_EPSILON: float = 1e-8  # guards against division by zero

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="depth_momentum",
    hypothesis=(
        "Rate of change in LOB depth imbalance predicts near-term price"
        " direction: accelerating bid-side depth growth signals upward"
        " pressure before price moves."
    ),
    formula="signal_t = EMA_8((depth_imb_t - depth_imb_{t-1}))",
    paper_refs=(),  # Original research
    data_fields=("bid_depth", "ask_depth"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class DepthMomentumAlpha:
    """O(1) depth-momentum predictor with EMA smoothing.

    Computes the first difference of depth imbalance and smooths via EMA.

    update() accepts either:
      - 2 positional args:  bid_depth, ask_depth
      - keyword args:       bid_depth=..., ask_depth=...
    """

    __slots__ = ("_prev_depth_imb", "_delta_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._prev_depth_imb: float = 0.0
        self._delta_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Ingest one tick of bid/ask depth and return the smoothed momentum signal."""
        # --- resolve bid_depth and ask_depth ---
        if len(args) >= 2:
            bid_depth = float(args[0])
            ask_depth = float(args[1])
        elif len(args) == 1:
            raise ValueError(
                "update() requires 2 positional args (bid_depth, ask_depth)"
                " or keyword args"
            )
        else:
            bid_depth = float(kwargs.get("bid_depth", 0.0))
            ask_depth = float(kwargs.get("ask_depth", 0.0))

        depth_imb = (bid_depth - ask_depth) / (bid_depth + ask_depth + _EPSILON)

        if not self._initialized:
            self._prev_depth_imb = depth_imb
            self._delta_ema = 0.0
            self._signal = 0.0
            self._initialized = True
            return 0.0

        delta = depth_imb - self._prev_depth_imb
        self._delta_ema += _EMA_ALPHA * (delta - self._delta_ema)
        self._prev_depth_imb = depth_imb
        self._signal = self._delta_ema
        return self._signal

    def reset(self) -> None:
        self._prev_depth_imb = 0.0
        self._delta_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = DepthMomentumAlpha

__all__ = ["DepthMomentumAlpha", "ALPHA_CLASS"]
