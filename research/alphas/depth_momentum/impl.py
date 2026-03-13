"""depth_momentum — Total Depth Change Rate Alpha.

Signal: Momentum of total depth change predicts liquidity regime shifts.
Rising total depth signals improving liquidity (mean-reverting),
falling depth signals deteriorating liquidity (trending).

References:
  Paper 013: arXiv 2512.11765 — HFT trading game transient impact

Formula:
  total     = bid_qty + ask_qty
  delta     = total - prev_total
  momentum += alpha16 * (delta - momentum)          # EMA-16 of delta
  baseline += alpha64 * (|delta| - baseline)        # EMA-64 of |delta|
  signal    = clip(momentum / max(baseline, eps), -2, 2)

Allocator Law : __slots__ on class; all state is scalar.
Precision Law : output is float (signal score, not price).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# EMA coefficients
# ---------------------------------------------------------------------------
_A16: float = 1.0 - math.exp(-1.0 / 16.0)  # ~0.0606 — momentum window
_A64: float = 1.0 - math.exp(-1.0 / 64.0)  # ~0.0154 — baseline window
_EPSILON: float = 1e-8

# ---------------------------------------------------------------------------
# Manifest (Allocator Law: cached at module level)
# ---------------------------------------------------------------------------
_MANIFEST = AlphaManifest(
    alpha_id="depth_momentum",
    hypothesis=(
        "Total depth change rate predicts liquidity regime shifts; "
        "rising total depth signals improving liquidity (mean-reverting), "
        "falling depth signals deteriorating liquidity (trending)."
    ),
    formula=(
        "signal = clip(EMA16(delta) / max(EMA64(|delta|), eps), -2, 2) where delta = (bid_qty + ask_qty) - prev_total"
    ),
    paper_refs=("013",),
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


# ---------------------------------------------------------------------------
# Alpha implementation
# ---------------------------------------------------------------------------
class DepthMomentumAlpha:
    """O(1) depth-momentum predictor with dual-EMA smoothing.

    State variables (all scalar, pre-allocated):
      _prev_total    : previous tick's total depth (bid+ask)
      _momentum_ema  : EMA-16 of depth change (delta)
      _baseline_ema  : EMA-64 of |delta| (normalizer)
      _signal        : cached output signal
      _initialized   : whether prev_total has been set
    """

    __slots__ = ("_prev_total", "_momentum_ema", "_baseline_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._prev_total: float = 0.0
        self._momentum_ema: float = 0.0
        self._baseline_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Update state and return the current signal.

        Accepts positional ``(bid_qty, ask_qty)`` or keyword args.
        Also accepts ``bids`` / ``asks`` array-like for protocol compat.
        """
        if len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif len(args) == 1:
            raise ValueError("update() requires 2 positional args (bid_qty, ask_qty) or keyword args")
        elif "bids" in kwargs and "asks" in kwargs:
            import numpy as np  # lazy; not on hot path in research mode

            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(np.asarray(bids).reshape(-1, 2)[0, 1])
            ask_qty = float(np.asarray(asks).reshape(-1, 2)[0, 1])
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        total = bid_qty + ask_qty

        if not self._initialized:
            self._prev_total = total
            self._initialized = True
            # First tick: delta=0, signal stays 0
            self._signal = 0.0
            return self._signal

        delta = total - self._prev_total
        self._prev_total = total

        # Momentum EMA (directional)
        self._momentum_ema += _A16 * (delta - self._momentum_ema)
        # Baseline EMA (magnitude normalizer)
        self._baseline_ema += _A64 * (abs(delta) - self._baseline_ema)

        # Normalized signal, clipped to [-2, 2]
        raw = self._momentum_ema / max(self._baseline_ema, _EPSILON)
        self._signal = max(-2.0, min(2.0, raw))
        return self._signal

    def reset(self) -> None:
        """Clear all state to initial values."""
        self._prev_total = 0.0
        self._momentum_ema = 0.0
        self._baseline_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = DepthMomentumAlpha

__all__ = ["DepthMomentumAlpha", "ALPHA_CLASS"]
