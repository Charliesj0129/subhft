"""ofi_regime — Regime-Dependent OFI Elasticity Alpha.

Signal: EMA-OFI amplified/attenuated by a rolling volatility regime factor.

References:
  Paper 123: arXiv 2505.17388v1 — OU/Lévy stochastic OFI dynamics, CSI 300 index futures
  Paper 122: OFI price impact coefficients

Formula:
  ofi       = (bid_qty - ask_qty) / max(bid_qty + ask_qty, 1)     ∈ [-1, 1]
  ofi_ema8  += α8  * (ofi - ofi_ema8)          # directional EMA (α8  ≈ 0.1175)
  vol16     += α16 * (|ofi| - vol16)            # short-term vol proxy (α16 ≈ 0.0606)
  base64    += α64 * (vol16 - base64)           # slow baseline (α64 ≈ 0.0154)
  rf        = clip(vol16 / max(base64, 1e-8), 0.5, 2.0)
  signal    = ofi_ema8 * rf                     ∈ [-2, 2]

Regime factor interpretation:
  rf ≈ 1.0 : neutral (current vol ≈ baseline)
  rf → 2.0 : high-vol regime — OFI more directional (Paper 123 β(high_vol))
  rf → 0.5 : low-vol regime  — OFI signal attenuated
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# EMA coefficients
# ---------------------------------------------------------------------------
_A8: float = 1.0 - math.exp(-1.0 / 8.0)    # ≈ 0.1175 — directional window
_A16: float = 1.0 - math.exp(-1.0 / 16.0)  # ≈ 0.0606 — short-term vol proxy
_A64: float = 1.0 - math.exp(-1.0 / 64.0)  # ≈ 0.0154 — slow baseline

_EPSILON: float = 1e-8

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
_MANIFEST = AlphaManifest(
    alpha_id="ofi_regime",
    hypothesis=(
        "OFI predictive power (impact coefficient) amplifies in high-vol regimes. "
        "Regime-scaled EMA-OFI outperforms flat EMA-OFI (Papers 123, 122)."
    ),
    formula=(
        "signal = EMA8(OFI_l1) * clip(EMA16(|OFI_l1|) / EMA64(EMA16(|OFI_l1|)), 0.5, 2.0)"
    ),
    paper_refs=("123", "122"),
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
class OfiRegimeAlpha:
    """Regime-dependent OFI elasticity alpha.

    Three float EMA states (pre-allocated, O(1) per tick):
      _ofi_ema8 : directional EMA of raw OFI
      _vol16    : short-term volatility proxy (EMA of |OFI|)
      _base64   : slow baseline of vol16
    """

    __slots__ = ("_ofi_ema8", "_vol16", "_base64", "_signal")

    def __init__(self) -> None:
        self._ofi_ema8: float = 0.0
        self._vol16: float = 0.0
        self._base64: float = 0.0
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
        ofi = (bid_qty - ask_qty) / max(total, 1.0)  # ∈ [-1, 1], guarded

        # Directional EMA
        self._ofi_ema8 += _A8 * (ofi - self._ofi_ema8)
        # Short-term volatility proxy (|OFI| magnitude)
        self._vol16 += _A16 * (abs(ofi) - self._vol16)
        # Slow baseline of the vol proxy
        self._base64 += _A64 * (self._vol16 - self._base64)

        # Regime factor: amplify in high-vol, attenuate in low-vol
        rf = self._vol16 / max(self._base64, _EPSILON)
        rf = max(0.5, min(2.0, rf))  # clip to [0.5, 2.0]

        self._signal = self._ofi_ema8 * rf
        return self._signal

    def reset(self) -> None:
        """Clear all EMA state to zero."""
        self._ofi_ema8 = 0.0
        self._vol16 = 0.0
        self._base64 = 0.0
        self._signal = 0.0

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal
