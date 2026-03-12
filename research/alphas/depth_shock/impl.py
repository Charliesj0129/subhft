"""Depth Shock Alpha — ref 080 (Directional Liquidity Shear).

Signal: Sudden depth drops on one side signal aggressive market orders and
predict short-term adverse selection.

Formula:
  d_bid  = bid_qty - prev_bid_qty
  d_ask  = ask_qty - prev_ask_qty
  shock  = min(d_bid, 0) - min(d_ask, 0)
  shock_ema     += alpha4  * (shock - shock_ema)
  shock_baseline += alpha32 * (|shock| - shock_baseline)
  signal = clip(shock_ema / max(shock_baseline, epsilon), -2, 2)

Interpretation:
  Negative d_bid (bid depth drops) = someone hit the bid -> bearish.
  Negative d_ask (ask depth drops) = someone lifted the ask -> bullish.
  shock = bid_drop - ask_drop:
    negative -> bid side hit harder -> bearish
    positive -> ask side hit harder -> bullish

HFT Laws compliance:
  - Allocator Law: __slots__ on class; all state is scalar, O(1) per tick.
  - Precision Law: output is float (signal score, not price).
  - Async Law: stateless per-tick computation, no blocking IO.
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay constants
_A4: float = 1.0 - math.exp(-1.0 / 4.0)  # ~0.2212 — fast shock EMA
_A32: float = 1.0 - math.exp(-1.0 / 32.0)  # ~0.0308 — slow baseline EMA

_EPSILON: float = 1e-8

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="depth_shock",
    hypothesis=(
        "Sudden depth drops on one side signal aggressive market orders and "
        "predict short-term adverse selection; bid-side shock implies downward "
        "pressure, ask-side shock implies upward pressure."
    ),
    formula=(
        "d_bid = bid - prev_bid; d_ask = ask - prev_ask; "
        "shock = min(d_bid, 0) - min(d_ask, 0); "
        "signal = clip(EMA4(shock) / max(EMA32(|shock|), eps), -2, 2)"
    ),
    paper_refs=("080",),
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


class DepthShockAlpha:
    """O(1) depth-shock adverse selection predictor.

    update() accepts either:
      - 2 positional args:  bid_qty, ask_qty
      - keyword args:       bid_qty=..., ask_qty=...
      - bids/asks arrays:   bids=np.ndarray (shape (N,2)), asks=np.ndarray (shape (N,2))
    """

    __slots__ = (
        "_prev_bid",
        "_prev_ask",
        "_shock_ema",
        "_shock_baseline",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        self._prev_bid: float = 0.0
        self._prev_ask: float = 0.0
        self._shock_ema: float = 0.0
        self._shock_baseline: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: ANN002
        """Update state and return the current signal."""
        # --- resolve bid_qty and ask_qty from various call conventions ---
        if args and len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif "bid_qty" in kwargs and "ask_qty" in kwargs:
            bid_qty = float(kwargs["bid_qty"])
            ask_qty = float(kwargs["ask_qty"])
        elif "bids" in kwargs and "asks" in kwargs:
            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(bids[0][1]) if hasattr(bids, "__getitem__") and len(bids) > 0 else 0.0  # type: ignore[arg-type]
            ask_qty = float(asks[0][1]) if hasattr(asks, "__getitem__") and len(asks) > 0 else 0.0  # type: ignore[arg-type]
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))  # type: ignore[arg-type]
            ask_qty = float(kwargs.get("ask_qty", 0.0))  # type: ignore[arg-type]

        # First tick: store prev values, return 0.0
        if not self._initialized:
            self._prev_bid = bid_qty
            self._prev_ask = ask_qty
            self._initialized = True
            return 0.0

        # Compute deltas
        d_bid = bid_qty - self._prev_bid
        d_ask = ask_qty - self._prev_ask

        # Store current as prev for next tick
        self._prev_bid = bid_qty
        self._prev_ask = ask_qty

        # Shock: only negative deltas matter (depth drops)
        shock = min(d_bid, 0.0) - min(d_ask, 0.0)

        # Update EMAs
        self._shock_ema += _A4 * (shock - self._shock_ema)
        self._shock_baseline += _A32 * (abs(shock) - self._shock_baseline)

        # Normalize and clip
        raw = self._shock_ema / max(self._shock_baseline, _EPSILON)
        self._signal = max(-2.0, min(2.0, raw))
        return self._signal

    def reset(self) -> None:
        """Clear all state to initial values."""
        self._prev_bid = 0.0
        self._prev_ask = 0.0
        self._shock_ema = 0.0
        self._shock_baseline = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = DepthShockAlpha

__all__ = ["DepthShockAlpha", "ALPHA_CLASS"]
