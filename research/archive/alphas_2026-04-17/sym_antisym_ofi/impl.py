"""Symmetric / Antisymmetric OFI Decomposition Alpha.

Decomposes L1 order-flow imbalance into two orthogonal components:

- **Antisymmetric OFI** = b_flow - a_flow  (directional pressure; same as standard OFI)
- **Symmetric OFI**     = b_flow + a_flow  (net liquidity provision/removal)

The symmetric component captures *overall market activity* — when both bid
and ask depth move in the same direction (both increasing = liquidity provision,
both decreasing = liquidity withdrawal).  This is orthogonal to the directional
signal captured by standard OFI.

b_flow / a_flow follow the Cont-Kukanov-Stoikov (2014) definition:
    if best_bid > prev_best_bid:  b_flow = bid_qty
    elif best_bid == prev_best_bid:  b_flow = bid_qty - prev_bid_qty
    else:  b_flow = -prev_bid_qty
    (analogous for ask side)

Paper refs:
    Elomari-Kessab (2024) — sym/antisym OFI decomposition.
    Cont, Kukanov, Stoikov (2014) — original OFI definition.

Allocator Law : __slots__, no heap allocations in update().
Precision Law : Prices are scaled int (x10000).  b_flow/a_flow are int quantities.
                Signal output is float (score, not price — Rule 11 exemption).
Cache Law     : Scalar state only (O(1) memory, no arrays needed for L1).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# EMA smoothing constants
# ---------------------------------------------------------------------------
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)
_SIGNAL_CLIP: float = 3.0
_WARMUP_TICKS: int = 32


_MANIFEST = AlphaManifest(
    alpha_id="sym_antisym_ofi",
    hypothesis=(
        "Decomposing L1 OFI into symmetric (b_flow + a_flow) and antisymmetric "
        "(b_flow - a_flow) components separates liquidity provision/removal "
        "from directional pressure.  The symmetric component should be "
        "orthogonal to standard OFI and may carry independent predictive "
        "information for short-horizon returns."
    ),
    formula=(
        "sym_ofi = b_flow + a_flow; "
        "antisym_ofi = b_flow - a_flow; "
        "signal_sym = EMA_8(sym_ofi), signal_antisym = EMA_8(antisym_ofi)"
    ),
    paper_refs=("Elomari-Kessab2024", "Cont-Kukanov-Stoikov2014"),
    data_fields=(
        "l1_bid_qty",
        "l1_ask_qty",
        "mid_price_x2",
        "spread_scaled",
    ),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile=None,
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v2",
)


class SymAntisymOfiAlpha:
    """O(1) symmetric + antisymmetric OFI decomposition.

    Emits symmetric OFI EMA as the primary signal.  The antisymmetric
    component is available via :meth:`get_antisym_signal` for diagnostic
    comparison but is identical to standard OFI.

    Requires keyword args: best_bid, best_ask, bid_qty, ask_qty (all int).
    """

    __slots__ = (
        "_prev_best_bid",
        "_prev_best_ask",
        "_prev_bid_qty",
        "_prev_ask_qty",
        "_sym_ema",
        "_antisym_ema",
        "_signal",
        "_antisym_signal",
        "_initialized",
        "_tick_count",
    )

    def __init__(self) -> None:
        self._prev_best_bid: int = 0
        self._prev_best_ask: int = 0
        self._prev_bid_qty: int = 0
        self._prev_ask_qty: int = 0
        self._sym_ema: float = 0.0
        self._antisym_ema: float = 0.0
        self._signal: float = 0.0
        self._antisym_signal: float = 0.0
        self._initialized: bool = False
        self._tick_count: int = 0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    @staticmethod
    def _compute_flows(
        *,
        best_bid: int,
        best_ask: int,
        bid_qty: int,
        ask_qty: int,
        prev_best_bid: int,
        prev_best_ask: int,
        prev_bid_qty: int,
        prev_ask_qty: int,
    ) -> tuple[int, int]:
        """Compute b_flow and a_flow per CKS 2014 definition.

        Returns (b_flow, a_flow).
        """
        # Bid side flow
        if best_bid > prev_best_bid:
            b_flow = bid_qty
        elif best_bid == prev_best_bid:
            b_flow = bid_qty - prev_bid_qty
        else:
            b_flow = -prev_bid_qty

        # Ask side flow
        if best_ask > prev_best_ask:
            a_flow = -prev_ask_qty
        elif best_ask == prev_best_ask:
            a_flow = ask_qty - prev_ask_qty
        else:
            a_flow = ask_qty

        return b_flow, a_flow

    def update(self, *args: float, **kwargs: object) -> float:
        """Update with new L1 book state.

        Required kwargs: best_bid, best_ask, bid_qty, ask_qty (all int).
        Returns the symmetric OFI EMA signal.
        """
        best_bid = int(kwargs["best_bid"])  # type: ignore[arg-type]
        best_ask = int(kwargs["best_ask"])  # type: ignore[arg-type]
        bid_qty = int(kwargs["bid_qty"])  # type: ignore[arg-type]
        ask_qty = int(kwargs["ask_qty"])  # type: ignore[arg-type]

        self._tick_count += 1

        if not self._initialized:
            # First tick: store state, emit zero signal.
            self._prev_best_bid = best_bid
            self._prev_best_ask = best_ask
            self._prev_bid_qty = bid_qty
            self._prev_ask_qty = ask_qty
            self._initialized = True
            self._signal = 0.0
            self._antisym_signal = 0.0
            return 0.0

        b_flow, a_flow = self._compute_flows(
            best_bid=best_bid,
            best_ask=best_ask,
            bid_qty=bid_qty,
            ask_qty=ask_qty,
            prev_best_bid=self._prev_best_bid,
            prev_best_ask=self._prev_best_ask,
            prev_bid_qty=self._prev_bid_qty,
            prev_ask_qty=self._prev_ask_qty,
        )

        # Decomposition
        sym_raw = float(b_flow + a_flow)
        antisym_raw = float(b_flow - a_flow)

        # EMA smoothing
        self._sym_ema += _EMA_ALPHA_8 * (sym_raw - self._sym_ema)
        self._antisym_ema += _EMA_ALPHA_8 * (antisym_raw - self._antisym_ema)

        # Store previous state
        self._prev_best_bid = best_bid
        self._prev_best_ask = best_ask
        self._prev_bid_qty = bid_qty
        self._prev_ask_qty = ask_qty

        # Warmup gate
        if self._tick_count < _WARMUP_TICKS:
            self._signal = 0.0
            self._antisym_signal = 0.0
        else:
            self._signal = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, self._sym_ema))
            self._antisym_signal = max(
                -_SIGNAL_CLIP, min(_SIGNAL_CLIP, self._antisym_ema)
            )

        return self._signal

    def get_signal(self) -> float:
        """Return the symmetric OFI EMA signal (primary)."""
        return self._signal

    def get_antisym_signal(self) -> float:
        """Return the antisymmetric OFI EMA signal (for diagnostic comparison)."""
        return self._antisym_signal

    def reset(self) -> None:
        self._prev_best_bid = 0
        self._prev_best_ask = 0
        self._prev_bid_qty = 0
        self._prev_ask_qty = 0
        self._sym_ema = 0.0
        self._antisym_ema = 0.0
        self._signal = 0.0
        self._antisym_signal = 0.0
        self._initialized = False
        self._tick_count = 0


ALPHA_CLASS = SymAntisymOfiAlpha

__all__ = ["SymAntisymOfiAlpha", "ALPHA_CLASS"]
