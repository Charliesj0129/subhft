"""Cross-Asset OFI Leader Alpha — ref 2112.13213 (Cont, Cucuringu, Zhang 2021).

Uses the order-flow imbalance of a sector leader (e.g. TSMC 2330) to predict
returns of follower stocks (2317 Hon Hai, 2454 MediaTek, 2881 Fubon).

Signal:
    COFI_t = α * self_OFI_t + (1-α) * leader_OFI_t

    where self_OFI = EMA_32( (Δbid_qty - Δask_qty) / (|Δbid_qty| + |Δask_qty| + 1) )
    and leader_OFI is the same formula computed on the leader symbol's L1 data.

Rationale (from paper):
    Lagged cross-asset OFIs improve forecasting of future returns.  The leader
    stock — typically the most liquid in its sector — incorporates information
    first.  Followers react with a delay, creating a predictable lead-lag
    pattern in order flow.

Implementation:
    The alpha accepts a `leader_ofi` kwarg in update() which the StrategyRunner
    provides from the leader symbol's computed OFI.  When leader_ofi is absent,
    the alpha falls back to self-OFI only.

Allocator Law  : __slots__, all scalar state, O(1) per tick.
Precision Law  : signal ∈ [-1, 1], float is fine.
Latency profile: shioaji_sim_p95_v2026-03-04.
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

_EMA_WINDOW: int = 32
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / _EMA_WINDOW)
_CROSS_WEIGHT: float = 0.5  # weight on leader OFI (vs self)

_MANIFEST = AlphaManifest(
    alpha_id="cross_ofi_leader",
    hypothesis=(
        "The OFI of a sector leader stock (e.g. TSMC) contains information "
        "about the near-future returns of follower stocks, because the leader "
        "incorporates market-wide information faster.  Combining leader OFI "
        "with self OFI improves IC by 40%+ over self-only."
    ),
    formula="COFI_t = 0.5 * self_OFI_t + 0.5 * leader_OFI_t",
    paper_refs=("2112.13213",),
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


class CrossOfiLeaderAlpha:
    """Cross-asset OFI alpha combining self and leader order-flow signals.

    update() accepts:
      - bid_qty, ask_qty (positional or keyword) — self symbol L1
      - leader_ofi (keyword, optional) — pre-computed leader OFI signal value
    """

    __slots__ = (
        "_prev_bid_qty",
        "_prev_ask_qty",
        "_self_ema",
        "_leader_ema",
        "_signal",
        "_initialized",
        "_tick_count",
        "_cross_weight",
    )

    def __init__(self, cross_weight: float = _CROSS_WEIGHT) -> None:
        self._prev_bid_qty: float = 0.0
        self._prev_ask_qty: float = 0.0
        self._self_ema: float = 0.0
        self._leader_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False
        self._tick_count: int = 0
        self._cross_weight: float = cross_weight

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args, **kwargs) -> float:  # noqa: C901
        # --- resolve bid_qty and ask_qty ---
        if len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif "bids" in kwargs and "asks" in kwargs:
            import numpy as np

            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(np.asarray(bids).reshape(-1, 2)[0, 1])
            ask_qty = float(np.asarray(asks).reshape(-1, 2)[0, 1])
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        # Optional leader signal
        leader_ofi = kwargs.get("leader_ofi", None)

        self._tick_count += 1

        if not self._initialized:
            self._prev_bid_qty = bid_qty
            self._prev_ask_qty = ask_qty
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # Self OFI (activity-normalized)
        d_bid = bid_qty - self._prev_bid_qty
        d_ask = ask_qty - self._prev_ask_qty
        a_mode = d_bid - d_ask
        activity = abs(d_bid) + abs(d_ask) + 1.0
        raw_self = a_mode / activity
        self._self_ema += _EMA_ALPHA * (raw_self - self._self_ema)

        # Leader EMA (smooth incoming leader signal)
        if leader_ofi is not None:
            self._leader_ema += _EMA_ALPHA * (float(leader_ofi) - self._leader_ema)

        # Combined signal
        w = self._cross_weight
        if leader_ofi is not None:
            self._signal = (1.0 - w) * self._self_ema + w * self._leader_ema
        else:
            self._signal = self._self_ema  # fallback: self-only

        self._prev_bid_qty = bid_qty
        self._prev_ask_qty = ask_qty

        return self._signal

    def reset(self) -> None:
        self._prev_bid_qty = 0.0
        self._prev_ask_qty = 0.0
        self._self_ema = 0.0
        self._leader_ema = 0.0
        self._signal = 0.0
        self._initialized = False
        self._tick_count = 0

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = CrossOfiLeaderAlpha

__all__ = ["CrossOfiLeaderAlpha", "ALPHA_CLASS"]
