"""Depth Velocity Diff Alpha — ref 039 (Intraday LOB Markov).

Hypothesis: Asymmetric rate of depth change (bid depth changing faster
than ask) indicates directional order flow not captured by level-based
imbalance.

Signal:
    d_bid  = bid_qty - prev_bid_qty
    d_ask  = ask_qty - prev_ask_qty
    diff   = d_bid - d_ask
    signal = clip(EMA_8(diff) / max(EMA_32(|diff|), epsilon), -2, 2)

A positive signal  -> bid depth growing faster -> upward pressure.
A negative signal  -> ask depth growing faster -> downward pressure.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay constants
_A8: float = 1.0 - math.exp(-1.0 / 8.0)
_A32: float = 1.0 - math.exp(-1.0 / 32.0)
_EPSILON: float = 1e-8

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="depth_velocity_diff",
    hypothesis=(
        "Asymmetric rate of depth change (bid depth changing faster than ask)"
        " indicates directional order flow not captured by level-based imbalance."
    ),
    formula=(
        "d_bid = bid - prev_bid; d_ask = ask - prev_ask; diff = d_bid - d_ask;"
        " signal = clip(EMA_8(diff) / max(EMA_32(|diff|), eps), -2, 2)"
    ),
    paper_refs=("039",),
    data_fields=("bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class DepthVelocityDiffAlpha:
    """O(1) depth-velocity-diff predictor with EMA smoothing and normalization.

    update() accepts either:
      - 2 positional args:  bid_qty, ask_qty
      - keyword args:       bid_qty=..., ask_qty=...
      - bids/asks arrays:   bids=np.ndarray (shape (N,2)), asks=np.ndarray (shape (N,2))
    """

    __slots__ = (
        "_prev_bid",
        "_prev_ask",
        "_diff_ema",
        "_abs_diff_baseline",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        self._prev_bid: float = 0.0
        self._prev_ask: float = 0.0
        self._diff_ema: float = 0.0
        self._abs_diff_baseline: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args, **kwargs) -> float:  # noqa: ANN002, ANN003
        """Ingest one tick of bid/ask depth and return the updated signal."""
        # --- resolve bid_qty and ask_qty from various call conventions ---
        if len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif len(args) == 1:
            raise ValueError(
                "update() requires 2 positional args (bid_qty, ask_qty)"
                " or keyword args"
            )
        elif "bids" in kwargs and "asks" in kwargs:
            import numpy as np  # lazy; not on hot path in research mode

            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(np.asarray(bids).reshape(-1, 2)[0, 1])
            ask_qty = float(np.asarray(asks).reshape(-1, 2)[0, 1])
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        if not self._initialized:
            # First tick: store prev values, no diff yet.
            self._prev_bid = bid_qty
            self._prev_ask = ask_qty
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # Compute depth velocity diff
        d_bid = bid_qty - self._prev_bid
        d_ask = ask_qty - self._prev_ask
        diff = d_bid - d_ask

        # Update EMAs
        self._diff_ema += _A8 * (diff - self._diff_ema)
        self._abs_diff_baseline += _A32 * (abs(diff) - self._abs_diff_baseline)

        # Normalize and clip
        denom = max(self._abs_diff_baseline, _EPSILON)
        raw = self._diff_ema / denom
        self._signal = max(-2.0, min(2.0, raw))

        # Store prev values for next tick
        self._prev_bid = bid_qty
        self._prev_ask = ask_qty

        return self._signal

    def reset(self) -> None:
        """Clear all internal state."""
        self._prev_bid = 0.0
        self._prev_ask = 0.0
        self._diff_ema = 0.0
        self._abs_diff_baseline = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        """Return the latest signal value."""
        return self._signal


ALPHA_CLASS = DepthVelocityDiffAlpha

__all__ = ["DepthVelocityDiffAlpha", "ALPHA_CLASS"]
