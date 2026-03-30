"""LOB Price-Keyed Kinetic Energy — static spatial structure signal.

Formulation (price-keyed, per team-lead Stage 2 spec):
  KE_bid = Σ_{i=1}^{5} bid_qty[i] × (mid - bid_price[i])²
  KE_ask = Σ_{i=1}^{5} ask_qty[i] × (ask_price[i] - mid)²
  LOB_momentum = (KE_bid - KE_ask) / (KE_bid + KE_ask + ε)   ∈ [-1, 1]
  LOB_gravity_center = Σ qty[i]*dist[i] / Σ qty[i]  (bid vs ask asymmetry)

This measures the *spatial distribution* of depth relative to mid-price,
NOT the kinetic energy from quantity changes (which is in impl.py).

Positive momentum = more energy (depth × distance²) on bid side = buy support.
Negative momentum = more energy on ask side = sell resistance.

Gravity center captures the average distance of liquidity from mid-price,
weighted by quantity. Asymmetry between bid and ask gravity centers
indicates directional bias.

Paper refs:
  Li, Cao, Polukarov, Ventre (2023) arXiv:2308.14235
  Bieganowski & Slepaczuk (2026) arXiv:2602.00776

Allocator Law  : __slots__; no heap allocations in update().
Precision Law  : output is float (signal score, not price).
Cache Law      : work arrays are contiguous float64.
"""

from __future__ import annotations

import math

import numpy as np

_N_LEVELS: int = 5
_EPSILON: float = 1e-12
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)  # ~8-tick EMA
_SIGNAL_CLIP: float = 1.0
_WARMUP_TICKS: int = 4  # minimal warmup (no velocity needed)


class LobPriceKeyedKE:
    """Price-keyed LOB kinetic energy and gravity center.

    Accepts bids/asks as shape (N, 2) arrays where col 0 = price, col 1 = qty.
    All prices should be in the same units (raw or scaled — doesn't matter
    as long as consistent).

    Outputs:
      momentum: normalized KE asymmetry in [-1, 1]
      gravity_center: bid_gc - ask_gc (positive = bid liquidity further from mid)
      ke_bid, ke_ask: raw kinetic energy values
    """

    __slots__ = (
        "_momentum_ema",
        "_gravity_ema",
        "_ke_bid",
        "_ke_ask",
        "_momentum",
        "_gravity_center",
        "_tick_count",
    )

    def __init__(self) -> None:
        self._momentum_ema: float = 0.0
        self._gravity_ema: float = 0.0
        self._ke_bid: float = 0.0
        self._ke_ask: float = 0.0
        self._momentum: float = 0.0
        self._gravity_center: float = 0.0
        self._tick_count: int = 0

    def update(
        self,
        bids: np.ndarray,
        asks: np.ndarray,
        mid_price: float | None = None,
    ) -> tuple[float, float]:
        """Update with LOB snapshot.

        Args:
            bids: shape (N, 2) — [[price, qty], ...] sorted best-to-worst
            asks: shape (N, 2) — [[price, qty], ...] sorted best-to-worst
            mid_price: if None, computed as (best_bid + best_ask) / 2

        Returns:
            (momentum, gravity_center) tuple
        """
        self._tick_count += 1

        n_bid = min(len(bids), _N_LEVELS)
        n_ask = min(len(asks), _N_LEVELS)

        if n_bid == 0 or n_ask == 0:
            return self._momentum_ema, self._gravity_ema

        if mid_price is None:
            mid_price = (bids[0, 0] + asks[0, 0]) * 0.5

        # --- KE computation (price-keyed) ---
        ke_bid = 0.0
        ke_ask = 0.0
        gc_bid_num = 0.0  # qty-weighted distance (bid side)
        gc_ask_num = 0.0  # qty-weighted distance (ask side)
        gc_bid_den = 0.0  # total qty (bid side)
        gc_ask_den = 0.0  # total qty (ask side)

        for i in range(n_bid):
            px = bids[i, 0]
            qty = bids[i, 1]
            dist = mid_price - px  # always >= 0 for bids
            ke_bid += qty * dist * dist
            gc_bid_num += qty * dist
            gc_bid_den += qty

        for i in range(n_ask):
            px = asks[i, 0]
            qty = asks[i, 1]
            dist = px - mid_price  # always >= 0 for asks
            ke_ask += qty * dist * dist
            gc_ask_num += qty * dist
            gc_ask_den += qty

        self._ke_bid = ke_bid
        self._ke_ask = ke_ask

        # --- Normalized momentum ---
        ke_total = ke_bid + ke_ask
        if ke_total > _EPSILON:
            raw_momentum = (ke_bid - ke_ask) / ke_total
        else:
            raw_momentum = 0.0

        # --- Gravity center asymmetry ---
        gc_bid = gc_bid_num / (gc_bid_den + _EPSILON)
        gc_ask = gc_ask_num / (gc_ask_den + _EPSILON)
        raw_gc = gc_bid - gc_ask  # positive = bid liquidity farther from mid

        # --- EMA smoothing ---
        if self._tick_count <= 1:
            self._momentum_ema = raw_momentum
            self._gravity_ema = raw_gc
        else:
            self._momentum_ema += _EMA_ALPHA * (raw_momentum - self._momentum_ema)
            self._gravity_ema += _EMA_ALPHA * (raw_gc - self._gravity_ema)

        # Clip
        self._momentum = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, self._momentum_ema))
        self._gravity_center = self._gravity_ema

        return self._momentum, self._gravity_center

    @property
    def momentum(self) -> float:
        return self._momentum

    @property
    def gravity_center(self) -> float:
        return self._gravity_center

    @property
    def ke_bid(self) -> float:
        return self._ke_bid

    @property
    def ke_ask(self) -> float:
        return self._ke_ask

    def reset(self) -> None:
        self._momentum_ema = 0.0
        self._gravity_ema = 0.0
        self._ke_bid = 0.0
        self._ke_ask = 0.0
        self._momentum = 0.0
        self._gravity_center = 0.0
        self._tick_count = 0


__all__ = ["LobPriceKeyedKE"]
