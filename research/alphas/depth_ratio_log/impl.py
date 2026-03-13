"""depth_ratio_log — Log Bid/Ask Depth Ratio Alpha.

Signal: log-ratio of best bid/ask depth, EMA-smoothed and clipped.

References:
  Paper 032: arXiv 2601.19369 — Directional liquidity geometric shear

Formula:
  log_ratio = log(max(bid_qty, 1) / max(ask_qty, 1))
  signal    = clip(EMA_8(log_ratio), -2, 2)

A positive signal indicates bid-side depth dominance (upward pressure).
A negative signal indicates ask-side depth dominance (downward pressure).
The log transform makes the measure symmetric and additive around zero.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_A8: float = 1.0 - math.exp(-1.0 / 8.0)

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
_MANIFEST = AlphaManifest(
    alpha_id="depth_ratio_log",
    hypothesis=(
        "Log-ratio of bid/ask depth is a symmetric measure of directional "
        "pressure; log transform makes it additive and centered at zero."
    ),
    formula="signal = clip(EMA_8(log(max(bid,1)/max(ask,1))), -2, 2)",
    paper_refs=("032",),
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
class DepthRatioLogAlpha:
    """O(1) log bid/ask depth ratio predictor with EMA smoothing.

    Three scalar state variables (pre-allocated, O(1) per tick):
      _ema         : EMA of log(bid/ask) ratio
      _signal      : cached clipped signal
      _initialized : whether first tick has been received
    """

    __slots__ = ("_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args, **kwargs) -> float:  # noqa: ANN002, ANN003
        """Update state and return the current signal.

        Accepts positional ``(bid_qty, ask_qty)`` or keyword args.
        Also accepts ``bids`` / ``asks`` array-like for protocol compat.
        """
        if len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif "bids" in kwargs and "asks" in kwargs:
            import numpy as np

            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(np.asarray(bids).reshape(-1, 2)[0, 1])
            ask_qty = float(np.asarray(asks).reshape(-1, 2)[0, 1])
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        # Log ratio: log(max(bid, 1) / max(ask, 1))
        log_ratio = math.log(max(bid_qty, 1.0) / max(ask_qty, 1.0))

        if not self._initialized:
            self._ema = log_ratio
            self._initialized = True
        else:
            self._ema += _A8 * (log_ratio - self._ema)

        # Clip to [-2, 2]
        self._signal = max(-2.0, min(2.0, self._ema))
        return self._signal

    def reset(self) -> None:
        """Clear all EMA state to zero."""
        self._ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = DepthRatioLogAlpha

__all__ = ["DepthRatioLogAlpha", "ALPHA_CLASS"]
