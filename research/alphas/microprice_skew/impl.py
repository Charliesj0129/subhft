"""Microprice Skew Alpha — normalized microprice deviation from midpoint.

Signal:  MS_t = EMA_8( (microprice - mid_price) / max(spread, epsilon) )

Where:   microprice = (ask_px * bid_qty + bid_px * ask_qty) / max(bid_qty + ask_qty, epsilon)
         spread = ask_px - bid_px

Hypothesis: the microprice (volume-weighted midpoint) deviates from the simple
midpoint when there is depth asymmetry.  The normalized deviation measures
information asymmetry and predicts price direction.  Positive = buy pressure
(microprice above mid), negative = sell pressure.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)

# Guard against division by zero.
_EPSILON: float = 1e-8

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="microprice_skew",
    hypothesis=(
        "The microprice (volume-weighted midpoint) deviates from the simple"
        " midpoint when there is depth asymmetry. The normalized deviation"
        " (microprice - midpoint) / spread measures information asymmetry"
        " and predicts price direction."
    ),
    formula="MS_t = EMA_8((microprice - mid_price) / max(spread, epsilon))",
    paper_refs=(),
    data_fields=("bid_px", "ask_px", "bid_qty", "ask_qty", "mid_price"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class MicropriceSkewAlpha:
    """O(1) microprice skew predictor with EMA smoothing.

    update() accepts either:
      - 5 positional args:  bid_px, ask_px, bid_qty, ask_qty, mid_price
      - keyword args:       bid_px=..., ask_px=..., bid_qty=..., ask_qty=..., mid_price=...
    """

    __slots__ = ("_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: ANN002
        """Update signal with new LOB data."""
        # --- resolve inputs ---
        if len(args) >= 5:
            bid_px = float(args[0])
            ask_px = float(args[1])
            bid_qty = float(args[2])
            ask_qty = float(args[3])
            mid_price = float(args[4])
        elif 1 <= len(args) < 5:
            raise ValueError(
                "update() requires 5 positional args "
                "(bid_px, ask_px, bid_qty, ask_qty, mid_price) or keyword args"
            )
        else:
            bid_px = float(kwargs.get("bid_px", 0.0))
            ask_px = float(kwargs.get("ask_px", 0.0))
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))
            mid_price = float(kwargs.get("mid_price", 0.0))

        # microprice = (ask_px * bid_qty + bid_px * ask_qty) / (bid_qty + ask_qty)
        total_qty = bid_qty + ask_qty
        microprice = (ask_px * bid_qty + bid_px * ask_qty) / max(total_qty, _EPSILON)

        # spread = ask_px - bid_px
        spread = ask_px - bid_px

        # Normalized skew: how far microprice deviates from midpoint, in spread units.
        raw_skew = (microprice - mid_price) / max(spread, _EPSILON)

        if not self._initialized:
            self._ema = raw_skew
            self._initialized = True
        else:
            self._ema += _EMA_ALPHA_8 * (raw_skew - self._ema)

        self._signal = self._ema
        return self._signal

    def reset(self) -> None:
        self._ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = MicropriceSkewAlpha

__all__ = ["MicropriceSkewAlpha", "ALPHA_CLASS"]
