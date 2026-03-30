"""Log-GOFI Alpha -- Logarithmic Generalized Order Flow Imbalance.

Signal:  log(1 + |OFI|) * sign(OFI)

Standard OFI is unbounded and heavy-tailed.  Large OFI values dominate
IC computation (Spearman rank correlation is somewhat robust, but extreme
values still distort signal-to-noise in downstream models).  The log
transform compresses extreme OFI while preserving sign and monotonicity.

Reference:
  Su (2021) -- logarithmic OFI normalization for improved alpha stability.

OFI from L1 depth changes (price-level aware):
  b_flow = delta(bid_qty) if bid_price unchanged, else +bid_qty / -bid_qty
  a_flow = -delta(ask_qty) if ask_price unchanged, else -ask_qty / +ask_qty
  ofi = b_flow - a_flow
  log_gofi = log(1 + |ofi|) * sign(ofi)

Uses math.log1p for numerical stability near zero.

Allocator Law  : __slots__ on class; no allocations in update().
Precision Law  : output is float (signal score, not price).
Cache Law      : scalar state only (O(1) memory).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

_MANIFEST = AlphaManifest(
    alpha_id="log_gofi",
    hypothesis=(
        "Logarithmic compression of OFI reduces noise from extreme "
        "order-flow values, improving information coefficient relative "
        "to raw OFI.  Heavy-tailed OFI distributions cause IC "
        "instability; log(1+|OFI|)*sign(OFI) stabilizes the signal."
    ),
    formula="log_gofi = log1p(|ofi|) * sign(ofi)",
    paper_refs=("Su2021",),
    data_fields=(
        "ofi_l1_raw",
        "l1_bid_qty",
        "l1_ask_qty",
        "bid_px",
        "ask_px",
    ),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v2",
)


def log_gofi_transform(ofi: float) -> float:
    """Apply log compression to OFI: log(1 + |ofi|) * sign(ofi).

    Uses math.log1p for numerical stability when |ofi| is small.
    """
    if ofi > 0.0:
        return math.log1p(ofi)
    elif ofi < 0.0:
        return -math.log1p(-ofi)
    return 0.0


class LogGofiAlpha:
    """O(1) log-compressed OFI signal from L1 depth changes.

    Requires bid/ask price and quantity to compute price-level-aware OFI,
    then applies log(1 + |OFI|) * sign(OFI) compression.
    """

    __slots__ = (
        "_prev_bid_px",
        "_prev_ask_px",
        "_prev_bid_qty",
        "_prev_ask_qty",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        self._prev_bid_px: int = 0
        self._prev_ask_px: int = 0
        self._prev_bid_qty: int = 0
        self._prev_ask_qty: int = 0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(
        self,
        *args: float,
        bid_px: float = 0.0,
        ask_px: float = 0.0,
        bid_qty: float = 0.0,
        ask_qty: float = 0.0,
        **kwargs: object,
    ) -> float:
        """Compute log-GOFI from L1 depth changes.

        Price-level-aware OFI:
        - If bid price unchanged: b_flow = delta(bid_qty)
        - If bid price increased:  b_flow = +bid_qty (new level)
        - If bid price decreased:  b_flow = -prev_bid_qty (level lost)
        Same logic (inverted) for ask side.
        """
        if not self._initialized:
            self._prev_bid_px = bid_px
            self._prev_ask_px = ask_px
            self._prev_bid_qty = bid_qty
            self._prev_ask_qty = ask_qty
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # Bid flow (price-level aware)
        if bid_px == self._prev_bid_px:
            b_flow = bid_qty - self._prev_bid_qty
        elif bid_px > self._prev_bid_px:
            b_flow = bid_qty
        else:
            b_flow = -self._prev_bid_qty

        # Ask flow (CKS 2014 convention — matches FeatureEngine._compute_ofi_l1_raw)
        if ask_px > self._prev_ask_px:
            a_flow = -self._prev_ask_qty
        elif ask_px == self._prev_ask_px:
            a_flow = ask_qty - self._prev_ask_qty
        else:
            a_flow = ask_qty

        ofi = b_flow - a_flow
        self._signal = log_gofi_transform(ofi)

        self._prev_bid_px = bid_px
        self._prev_ask_px = ask_px
        self._prev_bid_qty = bid_qty
        self._prev_ask_qty = ask_qty

        return self._signal

    def reset(self) -> None:
        self._prev_bid_px = 0
        self._prev_ask_px = 0
        self._prev_bid_qty = 0
        self._prev_ask_qty = 0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = LogGofiAlpha

__all__ = ["LogGofiAlpha", "ALPHA_CLASS", "log_gofi_transform"]
