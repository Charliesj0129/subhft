"""rough_vol_ofi -- Rough Volatility from Order Flow Alpha.

Signal: Estimates local volatility roughness from OFI autocorrelation structure.
"Rough" volatility (Hurst exponent H < 0.5) from order flow indicates
mean-reverting microstructure. When H is low (rough), the signal is contrarian;
when H is high (smooth/trending), the signal is momentum.

References:
  Paper 074: Rough Volatility from Order Flow

Formula:
  ofi         = bid_change - ask_change   (delta-based OFI)
  var_fast    = EMA_4((ofi - mean_fast)^2)
  var_slow    = EMA_16((ofi - mean_slow)^2)
  H           = clip(log(var_slow / var_fast) / (2 * log(4)), 0, 1)
  roughness   = 0.5 - H
  signal      = EMA_16(roughness * sign(ofi_ema))

Allocator Law : __slots__ on class; all state is scalar float.
Precision Law : output is float signal score (not price -- no Decimal needed).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# EMA coefficients
# ---------------------------------------------------------------------------
_FAST_ALPHA: float = 1.0 - math.exp(-1.0 / 4.0)    # ~0.2212 — fast variance scale
_SLOW_ALPHA: float = 1.0 - math.exp(-1.0 / 16.0)   # ~0.0606 — slow variance scale
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 16.0)     # ~0.0606 — signal smoothing
_OFI_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)  # ~0.1175 — OFI direction EMA

_EPSILON: float = 1e-12
_LOG_SCALE_RATIO: float = 2.0 * math.log(4.0)  # 2 * log(slow_window / fast_window)

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
_MANIFEST = AlphaManifest(
    alpha_id="rough_vol_ofi",
    hypothesis=(
        "Order flow volatility roughness (Hurst exponent < 0.5) indicates "
        "mean-reverting microstructure. Low Hurst combined with OFI direction "
        "provides a contrarian signal."
    ),
    formula="signal = EMA_16((0.5 - H) * sign(ofi_ema))",
    paper_refs=("074",),
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


# ---------------------------------------------------------------------------
# Alpha implementation
# ---------------------------------------------------------------------------
class RoughVolOfiAlpha:
    """O(1) rough-volatility OFI alpha with two-scale Hurst estimation.

    Pre-allocated scalar state (Allocator Law):
      _prev_bid, _prev_ask : previous tick bid/ask for delta computation
      _ofi_mean_fast/slow  : running mean of OFI at two scales
      _ofi_var_fast/slow   : running variance of OFI at two scales
      _ofi_ema             : directional EMA of OFI
      _hurst_ema           : smoothed Hurst estimate
      _signal              : final output
    """

    __slots__ = (
        "_ofi_ema", "_ofi_var_fast", "_ofi_var_slow",
        "_ofi_mean_fast", "_ofi_mean_slow", "_hurst_ema",
        "_signal", "_prev_bid", "_prev_ask", "_initialized",
    )

    def __init__(self) -> None:
        self._ofi_ema: float = 0.0
        self._ofi_var_fast: float = 0.0
        self._ofi_var_slow: float = 0.0
        self._ofi_mean_fast: float = 0.0
        self._ofi_mean_slow: float = 0.0
        self._hurst_ema: float = 0.5  # neutral default
        self._signal: float = 0.0
        self._prev_bid: float = 0.0
        self._prev_ask: float = 0.0
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
            self._signal = 0.0
            return self._signal

        # Compute OFI from bid/ask changes (delta-based)
        bid_change = bid_qty - self._prev_bid
        ask_change = ask_qty - self._prev_ask
        ofi = bid_change - ask_change

        self._prev_bid = bid_qty
        self._prev_ask = ask_qty

        # Update OFI directional EMA
        self._ofi_ema += _OFI_EMA_ALPHA * (ofi - self._ofi_ema)

        # Update fast-scale mean and variance: EMA_4
        self._ofi_mean_fast += _FAST_ALPHA * (ofi - self._ofi_mean_fast)
        dev_fast = ofi - self._ofi_mean_fast
        self._ofi_var_fast += _FAST_ALPHA * (dev_fast * dev_fast - self._ofi_var_fast)

        # Update slow-scale mean and variance: EMA_16
        self._ofi_mean_slow += _SLOW_ALPHA * (ofi - self._ofi_mean_slow)
        dev_slow = ofi - self._ofi_mean_slow
        self._ofi_var_slow += _SLOW_ALPHA * (dev_slow * dev_slow - self._ofi_var_slow)

        # Hurst estimate: H = log(var_slow / var_fast) / (2 * log(4))
        if self._ofi_var_fast > _EPSILON and self._ofi_var_slow > _EPSILON:
            ratio = self._ofi_var_slow / self._ofi_var_fast
            hurst_raw = math.log(ratio) / _LOG_SCALE_RATIO
            hurst_raw = max(0.0, min(1.0, hurst_raw))  # clip to [0, 1]
        else:
            hurst_raw = 0.5  # neutral when variance too small

        # Smooth Hurst estimate via EMA_16
        self._hurst_ema += _EMA_ALPHA * (hurst_raw - self._hurst_ema)

        # Roughness: positive when H < 0.5 (mean-reverting)
        roughness = 0.5 - self._hurst_ema

        # Directional signal: roughness * sign(ofi_ema)
        if self._ofi_ema > _EPSILON:
            ofi_sign = 1.0
        elif self._ofi_ema < -_EPSILON:
            ofi_sign = -1.0
        else:
            ofi_sign = 0.0

        raw_signal = roughness * ofi_sign

        # Clip to [-1, 1]
        self._signal = max(-1.0, min(1.0, raw_signal))
        return self._signal

    def reset(self) -> None:
        """Clear all state to defaults."""
        self._ofi_ema = 0.0
        self._ofi_var_fast = 0.0
        self._ofi_var_slow = 0.0
        self._ofi_mean_fast = 0.0
        self._ofi_mean_slow = 0.0
        self._hurst_ema = 0.5
        self._signal = 0.0
        self._prev_bid = 0.0
        self._prev_ask = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = RoughVolOfiAlpha

__all__ = ["RoughVolOfiAlpha", "ALPHA_CLASS"]
