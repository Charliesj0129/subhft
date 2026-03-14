"""Market Resistance Alpha — ref 098 (Concave Impact & Market Resistance).

Signal:  Detects endogenous market resistance by measuring how much price
         responds to cumulative order flow impact (OFI).

         When price response is concave (diminishing returns per unit of flow),
         sophisticated participants are absorbing flow — a contrarian signal.
         When convex (accelerating), momentum dominates.

Formula:
  ofi        = bid_change - ask_change           (tick-level OFI)
  cum_ofi    += α_cum * (ofi - cum_ofi)          (EMA-decayed cumulative OFI, α ≈ 0.0606)
  cum_dprice += α_cum * (Δmid - cum_dprice)      (EMA-decayed cumulative price change)
  ratio      = cum_ofi / (cum_dprice + ε)         (resistance ratio)
  baseline  += α_base * (ratio - baseline)        (slow-moving baseline, α ≈ 0.0154)
  signal     = EMA_16(ratio - baseline)           ∈ [-2, 2]

High ratio → lots of flow but little price movement → resistance (contrarian).
Low ratio  → price moves easily                    → momentum.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# EMA coefficients
# ---------------------------------------------------------------------------
_A16: float = 1.0 - math.exp(-1.0 / 16.0)   # ≈ 0.0606 — signal smoothing + cum decay
_A64: float = 1.0 - math.exp(-1.0 / 64.0)   # ≈ 0.0154 — slow baseline
_EPSILON: float = 1e-8  # guards against division by zero

# ---------------------------------------------------------------------------
# Manifest (Allocator Law: no per-call heap allocation)
# ---------------------------------------------------------------------------
_MANIFEST = AlphaManifest(
    alpha_id="market_resistance",
    hypothesis=(
        "When cumulative order flow produces diminishing price impact "
        "(concave response), sophisticated market participants are absorbing "
        "flow — signaling imminent price reversal."
    ),
    formula="signal = EMA_16(OFI_cum / (ΔPrice_cum + ε) - baseline)",
    paper_refs=("098",),
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


class MarketResistanceAlpha:
    """O(1) market resistance detector via EMA-decayed OFI/price-change ratio.

    update() accepts either:
      - keyword args: bid_qty=..., ask_qty=..., mid_price=...
      - positional args: bid_qty, ask_qty[, mid_price]
      - bids/asks arrays: bids=np.ndarray (shape (N,2)), asks=np.ndarray (shape (N,2))
    """

    __slots__ = (
        "_cum_ofi",
        "_cum_dprice",
        "_baseline",
        "_resistance_ema",
        "_signal",
        "_prev_bid",
        "_prev_ask",
        "_prev_mid",
        "_initialized",
    )

    def __init__(self) -> None:
        self._cum_ofi: float = 0.0
        self._cum_dprice: float = 0.0
        self._baseline: float = 0.0
        self._resistance_ema: float = 0.0
        self._signal: float = 0.0
        self._prev_bid: float = 0.0
        self._prev_ask: float = 0.0
        self._prev_mid: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: C901
        """Update state and return the current signal.

        Accepts positional ``(bid_qty, ask_qty[, mid_price])`` or keyword args.
        Also accepts ``bids`` / ``asks`` array-like for protocol compat.
        """
        # --- resolve bid_qty, ask_qty, mid_price ---
        if len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
            mid_price = float(args[2]) if len(args) >= 3 else None
        elif "bid_qty" in kwargs and "ask_qty" in kwargs:
            bid_qty = float(kwargs["bid_qty"])
            ask_qty = float(kwargs["ask_qty"])
            mid_price = float(kwargs["mid_price"]) if "mid_price" in kwargs else None
        elif "bids" in kwargs and "asks" in kwargs:
            bids = kwargs["bids"]
            asks = kwargs["asks"]
            has_bids = hasattr(bids, "__getitem__") and len(bids) > 0  # type: ignore[arg-type]
            has_asks = hasattr(asks, "__getitem__") and len(asks) > 0  # type: ignore[arg-type]
            bid_qty = float(bids[0][1]) if has_bids else 0.0  # type: ignore[arg-type]
            ask_qty = float(asks[0][1]) if has_asks else 0.0  # type: ignore[arg-type]
            bid_px = float(bids[0][0]) if has_bids else 0.0  # type: ignore[arg-type]
            ask_px = float(asks[0][0]) if has_asks else 0.0  # type: ignore[arg-type]
            mid_price = (bid_px + ask_px) / 2.0 if (bid_px + ask_px) > 0 else None
        else:
            bid_qty = 0.0
            ask_qty = 0.0
            mid_price = None

        # Derive mid_price if not explicitly provided and not from bids/asks arrays
        if mid_price is None:
            mid_price = 0.0

        if not self._initialized:
            self._prev_bid = bid_qty
            self._prev_ask = ask_qty
            self._prev_mid = mid_price
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # Compute tick-level OFI (bid change - ask change)
        ofi = (bid_qty - self._prev_bid) - (ask_qty - self._prev_ask)
        # Price change
        dprice = mid_price - self._prev_mid

        # Update prev state
        self._prev_bid = bid_qty
        self._prev_ask = ask_qty
        self._prev_mid = mid_price

        # EMA-decayed cumulative sums (avoids overflow vs raw sums)
        self._cum_ofi += _A16 * (ofi - self._cum_ofi)
        self._cum_dprice += _A16 * (dprice - self._cum_dprice)

        # Resistance ratio: high → flow absorbed, low → price moves easily
        ratio = self._cum_ofi / (abs(self._cum_dprice) + _EPSILON)

        # Slow-moving baseline
        self._baseline += _A64 * (ratio - self._baseline)

        # Signal = EMA of (ratio - baseline)
        deviation = ratio - self._baseline
        self._resistance_ema += _A16 * (deviation - self._resistance_ema)

        # Clip to [-2, 2]
        self._signal = max(-2.0, min(2.0, self._resistance_ema))
        return self._signal

    def reset(self) -> None:
        """Clear all state to zero."""
        self._cum_ofi = 0.0
        self._cum_dprice = 0.0
        self._baseline = 0.0
        self._resistance_ema = 0.0
        self._signal = 0.0
        self._prev_bid = 0.0
        self._prev_ask = 0.0
        self._prev_mid = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = MarketResistanceAlpha

__all__ = ["MarketResistanceAlpha", "ALPHA_CLASS"]
