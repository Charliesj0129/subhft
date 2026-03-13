"""Transient Impact Game Alpha — ref 013 (Obizhaeva-Wang).

Signal: Models transient price impact that decays over time.
        When estimated transient impact is high relative to total impact,
        the market is likely to revert (contrarian signal).
        When impact is low after large flow, it signals permanent information.

Formula:
  ofi       = delta(bid_qty) - delta(ask_qty)
  transient_impact = transient_impact * (1 - decay_rate) + abs(ofi)
  total_impact_ema += alpha * (abs(ofi) - total_impact_ema)
  ratio    = clip(-transient_impact / (total_impact_ema + epsilon), -1, 0)
  signal   = signal + alpha * (ratio - signal)            # EMA_8 smoothed

Interpretation:
  signal < 0 -> high transient impact -> expect reversion (contrarian)
  signal ~ 0 -> low transient / permanent information -> no clear edge

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price -- no Decimal needed).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)
_DECAY_RATE: float = 0.05  # transient impact decay per tick
_EPSILON: float = 1e-8  # guards against division by zero

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="transient_impact_game",
    hypothesis=(
        "Transient price impact from order flow decays predictably. "
        "High transient-to-total impact ratio signals temporary flow "
        "that will revert, providing a contrarian trading signal."
    ),
    formula="signal = EMA_8(-transient_impact / (total_impact + eps))",
    paper_refs=("013",),
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


class TransientImpactGameAlpha:
    """O(1) transient-impact contrarian signal with EMA smoothing.

    Tracks cumulative market impact that decays over time.  High
    transient-to-total ratio -> reversion expected (contrarian).

    update() accepts either:
      - 2 positional args:  bid_qty, ask_qty
      - keyword args:       bid_qty=..., ask_qty=...
      - bids/asks arrays:   bids=np.ndarray (shape (N,2)), asks=np.ndarray (shape (N,2))
    """

    __slots__ = (
        "_transient_impact",
        "_total_impact_ema",
        "_signal_ema",
        "_prev_bid",
        "_prev_ask",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        self._transient_impact: float = 0.0
        self._total_impact_ema: float = 0.0
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
        elif len(args) == 1:
            raise ValueError("update() requires 2 positional args (bid_qty, ask_qty) or keyword args")
        elif "bids" in kwargs and "asks" in kwargs:
            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(bids[0][1]) if hasattr(bids, "__getitem__") and len(bids) > 0 else 0.0  # type: ignore[arg-type]
            ask_qty = float(asks[0][1]) if hasattr(asks, "__getitem__") and len(asks) > 0 else 0.0  # type: ignore[arg-type]
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))  # type: ignore[arg-type]
            ask_qty = float(kwargs.get("ask_qty", 0.0))  # type: ignore[arg-type]

        # Compute OFI as change in L1 imbalance
        if not self._initialized:
            self._prev_bid = bid_qty
            self._prev_ask = ask_qty
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # OFI: change in bid minus change in ask (positive = buying pressure)
        ofi = (bid_qty - self._prev_bid) - (ask_qty - self._prev_ask)
        self._prev_bid = bid_qty
        self._prev_ask = ask_qty

        abs_ofi = abs(ofi)

        # Transient impact: decays each tick, excited by new flow
        self._transient_impact = self._transient_impact * (1.0 - _DECAY_RATE) + abs_ofi

        # Total impact EMA: tracks overall unsigned flow magnitude
        self._total_impact_ema += _EMA_ALPHA * (abs_ofi - self._total_impact_ema)

        # Ratio: how much of recent impact is transient vs total
        # Clip to [-1, 0] to prevent unbounded signal when both decay near zero
        ratio = -self._transient_impact / (self._total_impact_ema + _EPSILON)
        ratio = max(-1.0, min(0.0, ratio))

        # EMA-smooth the ratio for the final signal
        self._signal_ema += _EMA_ALPHA * (ratio - self._signal_ema)
        self._signal = self._signal_ema
        return self._signal

    def reset(self) -> None:
        """Clear all state to zero."""
        self._transient_impact = 0.0
        self._total_impact_ema = 0.0
        self._signal_ema = 0.0
        self._prev_bid = 0.0
        self._prev_ask = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = TransientImpactGameAlpha

__all__ = ["TransientImpactGameAlpha", "ALPHA_CLASS"]
