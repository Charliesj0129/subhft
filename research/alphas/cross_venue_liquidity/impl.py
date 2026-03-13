"""cross_venue_liquidity — Cross-Venue Liquidity Equilibrium Alpha.

Signal: Measures asymmetric liquidity replenishment rates between bid and ask
sides. Fast bid recovery relative to ask signals sustained buying interest.

References:
  Paper 062: Cross-Venue Liquidity Equilibrium

Formula:
  bid_recovery   = max(0, bid_qty - prev_bid)     # positive change = replenishment
  ask_recovery   = max(0, ask_qty - prev_ask)
  bid_recovery_ema += alpha4 * (bid_recovery - bid_recovery_ema)
  ask_recovery_ema += alpha4 * (ask_recovery - ask_recovery_ema)
  recovery_imbalance = (bid_recovery_ema - ask_recovery_ema) / (bid_recovery_ema + ask_recovery_ema + eps)
  signal = clip(EMA_8(recovery_imbalance), -1, 1)

Signal interpretation:
  signal > 0 : bid recovering faster → buying support
  signal < 0 : ask recovering faster → selling pressure
  signal ~ 0 : balanced recovery

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# EMA coefficients
# ---------------------------------------------------------------------------
_RECOVERY_ALPHA: float = 1.0 - math.exp(-1.0 / 4.0)  # ~0.2212 — fast recovery EMA
_SIGNAL_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)     # ~0.1175 — signal smoothing
_EPSILON: float = 1e-8

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
_MANIFEST = AlphaManifest(
    alpha_id="cross_venue_liquidity",
    hypothesis=(
        "Asymmetric liquidity replenishment rates between bid and ask sides "
        "reveal directional support: faster bid recovery signals sustained "
        "buying interest."
    ),
    formula="signal = EMA_8((bid_recovery - ask_recovery) / (total_recovery + eps))",
    paper_refs=("062",),
    data_fields=("bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.ENSEMBLE,
    rust_module=None,
    latency_profile=None,
    roles_used=("planner",),
    skills_used=("iterative-retrieval",),
    feature_set_version="lob_shared_v1",
)


# ---------------------------------------------------------------------------
# Alpha implementation
# ---------------------------------------------------------------------------
class CrossVenueLiquidityAlpha:
    """Cross-venue liquidity recovery imbalance alpha.

    Five scalar EMA states (pre-allocated, O(1) per tick):
      _bid_recovery_ema : EMA of positive bid qty changes (replenishment)
      _ask_recovery_ema : EMA of positive ask qty changes (replenishment)
      _signal_ema       : smoothed recovery imbalance
      _prev_bid         : previous tick bid qty
      _prev_ask         : previous tick ask qty
    """

    __slots__ = (
        "_bid_recovery_ema",
        "_ask_recovery_ema",
        "_signal_ema",
        "_prev_bid",
        "_prev_ask",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        self._bid_recovery_ema: float = 0.0
        self._ask_recovery_ema: float = 0.0
        self._signal_ema: float = 0.0
        self._prev_bid: float = 0.0
        self._prev_ask: float = 0.0
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
        if args and len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif "bid_qty" in kwargs and "ask_qty" in kwargs:
            bid_qty = float(kwargs["bid_qty"])
            ask_qty = float(kwargs["ask_qty"])
        elif "bids" in kwargs and "asks" in kwargs:
            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(bids[0][1]) if hasattr(bids, "__getitem__") and len(bids) > 0 else 0.0
            ask_qty = float(asks[0][1]) if hasattr(asks, "__getitem__") and len(asks) > 0 else 0.0
        else:
            bid_qty = 0.0
            ask_qty = 0.0

        if not self._initialized:
            self._prev_bid = bid_qty
            self._prev_ask = ask_qty
            self._initialized = True
            return 0.0

        # Compute recovery: positive qty changes = liquidity replenishment
        bid_recovery = max(0.0, bid_qty - self._prev_bid)
        ask_recovery = max(0.0, ask_qty - self._prev_ask)

        # EMA of recovery rates
        self._bid_recovery_ema += _RECOVERY_ALPHA * (bid_recovery - self._bid_recovery_ema)
        self._ask_recovery_ema += _RECOVERY_ALPHA * (ask_recovery - self._ask_recovery_ema)

        # Imbalance of recovery rates
        denom = self._bid_recovery_ema + self._ask_recovery_ema + _EPSILON
        recovery_imbalance = (self._bid_recovery_ema - self._ask_recovery_ema) / denom

        # EMA smooth
        self._signal_ema += _SIGNAL_ALPHA * (recovery_imbalance - self._signal_ema)
        self._signal = max(-1.0, min(1.0, self._signal_ema))

        self._prev_bid = bid_qty
        self._prev_ask = ask_qty
        return self._signal

    def reset(self) -> None:
        """Clear all EMA state to zero."""
        self._bid_recovery_ema = 0.0
        self._ask_recovery_ema = 0.0
        self._signal_ema = 0.0
        self._prev_bid = 0.0
        self._prev_ask = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = CrossVenueLiquidityAlpha

__all__ = ["CrossVenueLiquidityAlpha", "ALPHA_CLASS"]
