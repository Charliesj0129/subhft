"""SHAP Microstructure Alpha — ref 082 (Explainable Microstructure Patterns).

Signal: Weighted composite of three LOB microstructure features,
        smoothed via EMA(8).

  Feature 1 — imbalance:      (bid_qty - ask_qty) / (bid_qty + ask_qty)
  Feature 2 — spread_change:  delta of spread proxy (ask-bid) / total
  Feature 3 — vol_surprise:   (total - prev_total) / prev_total

  signal = EMA_8(0.45*imbalance + 0.30*spread_change + 0.25*vol_surprise)
  output clipped to [-2, 2]

Weights (w1=0.45, w2=0.30, w3=0.25) are empirically derived from
SHAP feature-importance analysis on short-term mid-price prediction.

Allocator Law  : __slots__ on class; all state is scalar (no heap alloc).
Precision Law  : output is float (signal score, not price — no Decimal needed).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)  # window ~8 ticks
_EPSILON: float = 1e-8

# SHAP-inspired feature weights (Paper 082)
_W_IMBALANCE: float = 0.45
_W_SPREAD_CHANGE: float = 0.30
_W_VOL_SURPRISE: float = 0.25

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="shap_microstructure",
    hypothesis=(
        "SHAP feature importance reveals that imbalance, spread dynamics, "
        "and volume surprise are the three most explanatory LOB features "
        "for short-term price prediction. A weighted composite captures "
        "their joint signal."
    ),
    formula="signal = EMA_8(0.45*imbalance + 0.30*spread_change + 0.25*vol_surprise)",
    paper_refs=("082",),
    data_fields=("bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.ENSEMBLE,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval",),
    feature_set_version="lob_shared_v1",
)


class ShapMicrostructureAlpha:
    """SHAP-weighted microstructure composite with EMA smoothing.

    Three scalar features (imbalance, spread proxy change, volume surprise)
    are combined with empirically-derived weights and smoothed via EMA(8).

    O(1) per tick, all state is scalar (__slots__).
    """

    __slots__ = (
        "_composite_ema",
        "_prev_spread_proxy",
        "_prev_total",
        "_signal",
        "_tick_count",
    )

    def __init__(self) -> None:
        self._composite_ema: float = 0.0
        self._prev_spread_proxy: float = 0.0
        self._prev_total: float = 0.0
        self._signal: float = 0.0
        self._tick_count: int = 0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Update state and return current signal.

        Accepts positional ``(bid_qty, ask_qty)`` or keyword args.
        Also accepts ``bids`` / ``asks`` array-like for protocol compat.
        """
        if len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif len(args) == 1:
            raise ValueError(
                "update() requires 2 positional args (bid_qty, ask_qty) "
                "or keyword args"
            )
        elif "bids" in kwargs and "asks" in kwargs:
            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = (
                float(bids[0][1])  # type: ignore[index]
                if hasattr(bids, "__getitem__") and len(bids) > 0  # type: ignore[arg-type]
                else 0.0
            )
            ask_qty = (
                float(asks[0][1])  # type: ignore[index]
                if hasattr(asks, "__getitem__") and len(asks) > 0  # type: ignore[arg-type]
                else 0.0
            )
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        total = bid_qty + ask_qty

        # Feature 1: order book imbalance
        imbalance = (bid_qty - ask_qty) / (total + _EPSILON)

        # Feature 2: spread proxy change
        spread_proxy = (ask_qty - bid_qty) / (total + _EPSILON)
        spread_change = spread_proxy - self._prev_spread_proxy

        # Feature 3: volume surprise (relative change in total depth)
        vol_surprise = (
            (total - self._prev_total) / (self._prev_total + _EPSILON)
            if self._tick_count > 0
            else 0.0
        )

        # Update lagged state
        self._prev_spread_proxy = spread_proxy
        self._prev_total = total

        # Weighted composite
        composite = (
            _W_IMBALANCE * imbalance
            + _W_SPREAD_CHANGE * spread_change
            + _W_VOL_SURPRISE * vol_surprise
        )

        # EMA smoothing
        if self._tick_count == 0:
            self._composite_ema = composite
        else:
            self._composite_ema += _EMA_ALPHA * (composite - self._composite_ema)

        self._tick_count += 1

        # Clip to [-2, 2]
        self._signal = max(-2.0, min(2.0, self._composite_ema))
        return self._signal

    def reset(self) -> None:
        """Clear all state to initial values."""
        self._composite_ema = 0.0
        self._prev_spread_proxy = 0.0
        self._prev_total = 0.0
        self._signal = 0.0
        self._tick_count = 0

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = ShapMicrostructureAlpha

__all__ = ["ShapMicrostructureAlpha", "ALPHA_CLASS"]
