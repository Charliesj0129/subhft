"""Adverse Selection Momentum Alpha.

Signal: AM_t = EMA_8(delta_price * lagged_imbalance)
  where delta_price   = mid_price_t - mid_price_{t-1}
        lagged_imbalance = (bid_qty_{t-1} - ask_qty_{t-1})
                         / (bid_qty_{t-1} + ask_qty_{t-1})

Hypothesis: Adverse selection creates momentum when informed traders
consistently move the price against market makers.  Tracking the
correlation between order flow direction (lagged imbalance) and
subsequent price moves reveals informed trading.  A positive signal
means flow is correctly predicting price moves.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price -- no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)
_EPSILON: float = 1e-8

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="adverse_momentum",
    hypothesis=(
        "Adverse selection creates momentum when informed traders"
        " consistently move the price against market makers."
        " Tracking the correlation between order flow direction"
        " and subsequent price moves reveals informed trading."
    ),
    formula="AM_t = EMA_8(delta_price * lagged_imbalance)",
    paper_refs=("131", "136"),
    data_fields=("mid_price", "bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class AdverseMomentumAlpha:
    """O(1) adverse-selection momentum predictor.

    update() accepts either:
      - 3 positional args:  mid_price, bid_qty, ask_qty
      - keyword args:       mid_price=..., bid_qty=..., ask_qty=...
    """

    __slots__ = (
        "_prev_mid",
        "_lagged_imbalance",
        "_ema",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        self._prev_mid: float = 0.0
        self._lagged_imbalance: float = 0.0
        self._ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Ingest one tick and return the updated signal.

        Args:
            mid_price: current mid price (scaled int or float).
            bid_qty: best-bid queue size.
            ask_qty: best-ask queue size.
        """
        if args:
            if len(args) < 3:
                raise ValueError(
                    "update() requires 3 positional args"
                    " (mid_price, bid_qty, ask_qty) or keyword args"
                )
            mid_price = float(args[0])
            bid_qty = float(args[1])
            ask_qty = float(args[2])
        else:
            mid_price = float(kwargs.get("mid_price", 0.0))
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        if not self._initialized:
            # First tick: no previous mid or imbalance, signal stays 0.
            self._prev_mid = mid_price
            # Compute imbalance for use as lagged value on next tick.
            denom = bid_qty + ask_qty
            self._lagged_imbalance = (bid_qty - ask_qty) / (denom + _EPSILON)
            self._initialized = True
            return self._signal

        # Step 1: price change
        delta_price = mid_price - self._prev_mid
        self._prev_mid = mid_price

        # Step 2: raw signal = delta_price * lagged_imbalance
        raw = delta_price * self._lagged_imbalance

        # Step 3: update lagged imbalance for next tick
        denom = bid_qty + ask_qty
        self._lagged_imbalance = (bid_qty - ask_qty) / (denom + _EPSILON)

        # Step 4: EMA smoothing and clip to [-2, 2]
        self._ema += _EMA_ALPHA * (raw - self._ema)
        self._signal = max(-2.0, min(2.0, self._ema))
        return self._signal

    def reset(self) -> None:
        self._prev_mid = 0.0
        self._lagged_imbalance = 0.0
        self._ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = AdverseMomentumAlpha

__all__ = ["AdverseMomentumAlpha", "ALPHA_CLASS"]
