"""regime_momentum — Regime Momentum Alpha.

Signal: Momentum of the volatility regime factor, directionally filtered by OFI sign.

References:
  Paper 082: explainable crypto microstructure — regime transitions predict directional pressure

Formula:
  ofi       = (bid_qty - ask_qty) / max(bid_qty + ask_qty, 1)     in [-1, 1]
  ofi_ema8  += A8  * (ofi - ofi_ema8)          # directional EMA (A8  ~ 0.1175)
  vol16     += A16 * (|ofi| - vol16)            # short-term vol proxy (A16 ~ 0.0606)
  base64    += A64 * (vol16 - base64)           # slow baseline (A64 ~ 0.0154)
  rf        = clip(vol16 / max(base64, eps), 0.5, 2.0)
  rf_ema8   += A8  * (rf - rf_ema8)            # fast regime factor EMA
  rf_ema32  += A32 * (rf - rf_ema32)           # slow regime factor EMA
  rf_momentum = rf_ema8 - rf_ema32
  signal    = clip(rf_momentum * (1.0 if ofi_ema8 >= 0 else -1.0), -2, 2)

Regime momentum interpretation:
  rf_momentum > 0 : regime factor accelerating (vol rising vs baseline)
  rf_momentum < 0 : regime factor decelerating (vol falling vs baseline)
  OFI sign filter : aligns momentum direction with order flow
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# EMA coefficients
# ---------------------------------------------------------------------------
_A8: float = 1.0 - math.exp(-1.0 / 8.0)  # ~ 0.1175 — fast window
_A16: float = 1.0 - math.exp(-1.0 / 16.0)  # ~ 0.0606 — short-term vol proxy
_A32: float = 1.0 - math.exp(-1.0 / 32.0)  # ~ 0.0308 — slow regime factor EMA
_A64: float = 1.0 - math.exp(-1.0 / 64.0)  # ~ 0.0154 — slow baseline

_EPSILON: float = 1e-8

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
_MANIFEST = AlphaManifest(
    alpha_id="regime_momentum",
    hypothesis=(
        "The momentum of the volatility regime factor predicts whether the market "
        "is transitioning between regimes; rising regime factor with positive OFI "
        "signals trend continuation."
    ),
    formula=("signal = clip((EMA8(rf) - EMA32(rf)) * sign(ofi_ema8), -2, 2) where rf = clip(vol16 / base64, 0.5, 2.0)"),
    paper_refs=("082",),
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
class RegimeMomentumAlpha:
    """Regime momentum alpha.

    Six float EMA states (pre-allocated, O(1) per tick):
      _ofi_ema8  : directional EMA of raw OFI
      _vol16     : short-term volatility proxy (EMA of |OFI|)
      _base64    : slow baseline of vol16
      _rf_ema8   : fast EMA of the regime factor
      _rf_ema32  : slow EMA of the regime factor
      _signal    : cached signal value
    """

    __slots__ = ("_ofi_ema8", "_vol16", "_base64", "_rf_ema8", "_rf_ema32", "_signal")

    def __init__(self) -> None:
        self._ofi_ema8: float = 0.0
        self._vol16: float = 0.0
        self._base64: float = 0.0
        self._rf_ema8: float = 1.0
        self._rf_ema32: float = 1.0
        self._signal: float = 0.0

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
            # Accept array-like (e.g. from BidAskEvent)
            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(bids[0][1]) if hasattr(bids, "__getitem__") and len(bids) > 0 else 0.0
            ask_qty = float(asks[0][1]) if hasattr(asks, "__getitem__") and len(asks) > 0 else 0.0
        else:
            bid_qty = 0.0
            ask_qty = 0.0

        total = bid_qty + ask_qty
        ofi = (bid_qty - ask_qty) / max(total, 1.0)  # in [-1, 1], guarded

        # Directional EMA
        self._ofi_ema8 += _A8 * (ofi - self._ofi_ema8)
        # Short-term volatility proxy (|OFI| magnitude)
        self._vol16 += _A16 * (abs(ofi) - self._vol16)
        # Slow baseline of the vol proxy
        self._base64 += _A64 * (self._vol16 - self._base64)

        # Regime factor: amplify in high-vol, attenuate in low-vol
        rf = self._vol16 / max(self._base64, _EPSILON)
        rf = max(0.5, min(2.0, rf))  # clip to [0.5, 2.0]

        # Regime factor momentum (fast - slow EMA of rf)
        self._rf_ema8 += _A8 * (rf - self._rf_ema8)
        self._rf_ema32 += _A32 * (rf - self._rf_ema32)

        rf_momentum = self._rf_ema8 - self._rf_ema32

        # Directional filter: align momentum with OFI sign
        direction = 1.0 if self._ofi_ema8 >= 0.0 else -1.0
        self._signal = max(-2.0, min(2.0, rf_momentum * direction))
        return self._signal

    def reset(self) -> None:
        """Clear all EMA state to initial values."""
        self._ofi_ema8 = 0.0
        self._vol16 = 0.0
        self._base64 = 0.0
        self._rf_ema8 = 1.0
        self._rf_ema32 = 1.0
        self._signal = 0.0

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = RegimeMomentumAlpha

__all__ = ["RegimeMomentumAlpha", "ALPHA_CLASS", "_MANIFEST", "_A8", "_A16", "_A32", "_A64"]
