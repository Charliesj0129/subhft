"""LOB Kinetic Energy Alpha — physics-inspired LOB dynamics signal.

Signal: Treats limit order book quantities as particles in a physical system.
Computes "kinetic energy" (0.5 * q * v^2) and "momentum" (q * v) from the
rate of change of quantities at each book level.

- KE_bid = 0.5 * sum_i q_i * v_i^2  (bid side)
- KE_ask = 0.5 * sum_i q_i * v_i^2  (ask side)
- Momentum P = sum(q_i * v_i) bid - sum(q_i * v_i) ask
- Active depth: configurable level range (default levels 1-5; skip_l1=True uses 2-5)
- BBO-shift guard: zeroes velocity at levels where best price changed between ticks

The directional signal is the normalized momentum:
  signal = P / (KE_bid + KE_ask + epsilon)

Energy asymmetry between bid/ask sides predicts directional moves.
High total energy predicts volatility expansion.

Paper refs:
  Li, Cao, Polukarov, Ventre (2023) arXiv:2308.14235 — "An Empirical Analysis
  on Financial Markets: Insights from the Application of Statistical Physics".
  L3 order book data, kinetic energy / momentum / active depth concept.

Challenger conditions addressed:
  C1: compute_mldm_correlation() method for demonstrating distinctness.
  C2: 5-30s prediction horizon (signal is tick-to-tick, holding period configurable).

Allocator Law  : __slots__; pre-allocated numpy ring buffers (no list.append).
Precision Law  : output is float (signal score, not price).
Cache Law      : prev/cur qty arrays are contiguous float64 (5 elements each).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""

from __future__ import annotations

import math

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# --- Constants ---
_N_LEVELS: int = 5
_EPSILON: float = 1e-12
_EMA_FAST: float = 1.0 - math.exp(-1.0 / 4.0)   # ~4-tick EMA
_EMA_SLOW: float = 1.0 - math.exp(-1.0 / 16.0)   # ~16-tick EMA
_SIGNAL_CLIP: float = 2.0
_WARMUP_TICKS: int = 16  # need >= 2 ticks for velocity, plus stabilization
_DEFAULT_ACTIVE_DEPTH: int = 5  # use all 5 levels by default
_RING_SIZE: int = 1024  # pre-allocated ring buffer size for history

_MANIFEST = AlphaManifest(
    alpha_id="lob_kinetic_energy",
    hypothesis=(
        "LOB quantity dynamics, modeled as kinetic energy and momentum of a "
        "particle system, capture directional pressure that static shape "
        "measures (convexity, imbalance) miss.  Momentum asymmetry between "
        "bid/ask sides predicts 5-30s price direction.  Active depth filtering "
        "removes noise from deep levels that have no price impact.  skip_l1 "
        "option excludes L1 to reduce OFI-family correlation.  BBO-shift guard "
        "zeroes velocity on price-level shifts to prevent spurious v^2 spikes.  "
        "Distinct from mldm_depth_momentum (per-level tracking) because KE uses "
        "physics-inspired q*v^2 weighting and aggregate momentum."
    ),
    formula=(
        "KE_side = 0.5 * sum_i q_i * v_i^2, "
        "P = sum(q_i * v_i)_bid - sum(q_i * v_i)_ask, "
        "signal = EMA_4(P / (KE_total + eps)), clipped to [-2, 2]"
    ),
    paper_refs=("arXiv:2308.14235",),
    data_fields=("bid_px", "ask_px", "bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class LobKineticEnergyAlpha:
    """Physics-inspired LOB dynamics alpha using kinetic energy and momentum.

    Requires bids/asks keyword args with multi-level depth data.
    Outputs normalized momentum signal in [-2, 2].
    Positive = bid-side energy dominance (buy pressure).
    Negative = ask-side energy dominance (sell pressure).

    Args:
        active_depth: Number of book levels to use (1-5, default 5).
        skip_l1: If True, exclude level 1 (BBO) from KE/momentum to reduce
            correlation with OFI-family alphas (default False).
        ring_size: Size of pre-allocated ring buffers for history (default 1024).
    """

    __slots__ = (
        "_active_depth",
        "_skip_l1",
        "_level_start",
        "_prev_bid_qty",
        "_prev_ask_qty",
        "_prev_bid_price",
        "_prev_ask_price",
        "_cur_bid_qty",
        "_cur_ask_qty",
        "_velocity_bid",
        "_velocity_ask",
        "_momentum_ema",
        "_energy_ema",
        "_signal",
        "_initialized",
        "_tick_count",
        "_ring_ke_bid",
        "_ring_ke_ask",
        "_ring_momentum",
        "_ring_head",
        "_ring_count",
        "_ring_size",
    )

    def __init__(
        self,
        active_depth: int = _DEFAULT_ACTIVE_DEPTH,
        skip_l1: bool = False,
        ring_size: int = _RING_SIZE,
    ) -> None:
        self._active_depth: int = min(active_depth, _N_LEVELS)
        self._skip_l1: bool = skip_l1
        self._level_start: int = 1 if skip_l1 else 0
        # Pre-allocated arrays (Allocator Law)
        self._prev_bid_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._prev_ask_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._prev_bid_price: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._prev_ask_price: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._cur_bid_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._cur_ask_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._velocity_bid: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._velocity_ask: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        # EMA state
        self._momentum_ema: float = 0.0
        self._energy_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False
        self._tick_count: int = 0
        # Pre-allocated ring buffers for history (Allocator Law fix)
        self._ring_size: int = ring_size
        self._ring_ke_bid: np.ndarray = np.zeros(ring_size, dtype=np.float64)
        self._ring_ke_ask: np.ndarray = np.zeros(ring_size, dtype=np.float64)
        self._ring_momentum: np.ndarray = np.zeros(ring_size, dtype=np.float64)
        self._ring_head: int = 0
        self._ring_count: int = 0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def _store_history(self, ke_bid: float, ke_ask: float, momentum: float) -> None:
        """Write to pre-allocated ring buffers with modulo indexing."""
        idx = self._ring_head % self._ring_size
        self._ring_ke_bid[idx] = ke_bid
        self._ring_ke_ask[idx] = ke_ask
        self._ring_momentum[idx] = momentum
        self._ring_head += 1
        if self._ring_count < self._ring_size:
            self._ring_count += 1

    def _get_momentum_history(self, n: int) -> np.ndarray:
        """Return last n momentum values from ring buffer."""
        count = min(n, self._ring_count)
        if count == 0:
            return np.empty(0, dtype=np.float64)
        result = np.empty(count, dtype=np.float64)
        head = self._ring_head
        for i in range(count):
            idx = (head - count + i) % self._ring_size
            result[i] = self._ring_momentum[idx]
        return result

    def update(self, *args: float, **kwargs: object) -> float:
        """Update with LOB snapshot. Accepts bids= and asks= kwargs.

        bids/asks: np.ndarray shape (N, 2) where col 0 = price, col 1 = quantity.
        """
        cur_bid = self._cur_bid_qty
        cur_ask = self._cur_ask_qty
        cur_bid[:] = 0.0
        cur_ask[:] = 0.0

        # Extract current prices for BBO-shift guard
        cur_bid_price = np.zeros(_N_LEVELS, dtype=np.float64)
        cur_ask_price = np.zeros(_N_LEVELS, dtype=np.float64)

        if "bids" in kwargs and "asks" in kwargs:
            # Multi-level book format: shape (N, 2) = (price, qty)
            bids = np.asarray(kwargs["bids"], dtype=np.float64).reshape(-1, 2)
            asks = np.asarray(kwargs["asks"], dtype=np.float64).reshape(-1, 2)
            n_bid = min(bids.shape[0], _N_LEVELS)
            n_ask = min(asks.shape[0], _N_LEVELS)
            cur_bid[:n_bid] = bids[:n_bid, 1]
            cur_ask[:n_ask] = asks[:n_ask, 1]
            cur_bid_price[:n_bid] = bids[:n_bid, 0]
            cur_ask_price[:n_ask] = asks[:n_ask, 0]
        elif "bid_px" in kwargs and "ask_px" in kwargs:
            # L1 flat format: bid_px, ask_px, bid_qty, ask_qty (scalars)
            cur_bid[0] = float(kwargs.get("bid_qty", 0) or 0)  # type: ignore[arg-type]
            cur_ask[0] = float(kwargs.get("ask_qty", 0) or 0)  # type: ignore[arg-type]
            cur_bid_price[0] = float(kwargs.get("bid_px", 0) or 0)  # type: ignore[arg-type]
            cur_ask_price[0] = float(kwargs.get("ask_px", 0) or 0)  # type: ignore[arg-type]
        else:
            raise ValueError(
                "lob_kinetic_energy requires either bids=/asks= (multi-level) "
                "or bid_px=/ask_px=/bid_qty=/ask_qty= (L1) keyword args."
            )

        self._tick_count += 1

        if not self._initialized:
            np.copyto(self._prev_bid_qty, cur_bid)
            np.copyto(self._prev_ask_qty, cur_ask)
            np.copyto(self._prev_bid_price, cur_bid_price)
            np.copyto(self._prev_ask_price, cur_ask_price)
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # Compute velocity: v_i = delta_q_i (per tick, dt=1)
        ad = self._active_depth
        ls = self._level_start
        v_bid = self._velocity_bid
        v_ask = self._velocity_ask
        v_bid[:] = 0.0
        v_ask[:] = 0.0

        for i in range(ls, ad):
            # BBO-shift guard: if price at this level changed, the quantity
            # delta is meaningless (level shifted, not replenished/depleted).
            # Zero out velocity to prevent spurious v^2 spikes.
            bid_price_shifted = cur_bid_price[i] != self._prev_bid_price[i]
            ask_price_shifted = cur_ask_price[i] != self._prev_ask_price[i]

            if not bid_price_shifted:
                v_bid[i] = cur_bid[i] - self._prev_bid_qty[i]
            if not ask_price_shifted:
                v_ask[i] = cur_ask[i] - self._prev_ask_qty[i]

        # Kinetic energy: KE = 0.5 * sum(q * v^2) for active levels
        ke_bid = 0.0
        ke_ask = 0.0
        momentum_bid = 0.0
        momentum_ask = 0.0
        for i in range(ls, ad):
            q_b = cur_bid[i]
            q_a = cur_ask[i]
            vb = v_bid[i]
            va = v_ask[i]
            ke_bid += q_b * vb * vb
            ke_ask += q_a * va * va
            momentum_bid += q_b * vb
            momentum_ask += q_a * va
        ke_bid *= 0.5
        ke_ask *= 0.5

        # Net momentum: bid pressure - ask pressure
        momentum = momentum_bid - momentum_ask
        ke_total = ke_bid + ke_ask

        # Store in ring buffer (no heap allocation)
        self._store_history(ke_bid, ke_ask, momentum)

        # Normalized momentum
        norm_momentum = momentum / (ke_total + _EPSILON) if ke_total > _EPSILON else 0.0

        # EMA smoothing
        self._momentum_ema += _EMA_FAST * (norm_momentum - self._momentum_ema)
        self._energy_ema += _EMA_SLOW * (ke_total - self._energy_ema)

        # Update previous quantities and prices
        np.copyto(self._prev_bid_qty, cur_bid)
        np.copyto(self._prev_ask_qty, cur_ask)
        np.copyto(self._prev_bid_price, cur_bid_price)
        np.copyto(self._prev_ask_price, cur_ask_price)

        # Signal with warmup guard
        if self._tick_count < _WARMUP_TICKS:
            self._signal = 0.0
        else:
            self._signal = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, self._momentum_ema))

        return self._signal

    def reset(self) -> None:
        self._prev_bid_qty[:] = 0.0
        self._prev_ask_qty[:] = 0.0
        self._prev_bid_price[:] = 0.0
        self._prev_ask_price[:] = 0.0
        self._cur_bid_qty[:] = 0.0
        self._cur_ask_qty[:] = 0.0
        self._velocity_bid[:] = 0.0
        self._velocity_ask[:] = 0.0
        self._momentum_ema = 0.0
        self._energy_ema = 0.0
        self._signal = 0.0
        self._initialized = False
        self._tick_count = 0
        self._ring_ke_bid[:] = 0.0
        self._ring_ke_ask[:] = 0.0
        self._ring_momentum[:] = 0.0
        self._ring_head = 0
        self._ring_count = 0

    def get_signal(self) -> float:
        return self._signal

    def get_ke_bid(self) -> float:
        """Return latest bid-side kinetic energy (for analysis)."""
        if self._ring_count == 0:
            return 0.0
        idx = (self._ring_head - 1) % self._ring_size
        return float(self._ring_ke_bid[idx])

    def get_ke_ask(self) -> float:
        """Return latest ask-side kinetic energy (for analysis)."""
        if self._ring_count == 0:
            return 0.0
        idx = (self._ring_head - 1) % self._ring_size
        return float(self._ring_ke_ask[idx])

    def get_energy_ema(self) -> float:
        """Return EMA of total kinetic energy (volatility proxy)."""
        return self._energy_ema

    # --- Challenger C1: Correlation with mldm_depth_momentum ---
    def compute_mldm_correlation(self, mldm_signals: list[float]) -> dict[str, float]:
        """Compute correlation between this alpha's momentum and mldm_depth_momentum.

        Must show correlation < 0.7 to demonstrate distinctness.

        Key differences from mldm_depth_momentum:
        1. mldm tracks per-level depth changes independently.
           KE uses q*v^2 weighting -- large quantities with large velocity
           contribute quadratically more.
        2. mldm sums delta_q directly.  KE momentum is q*v (quantity-weighted
           velocity), not just velocity.
        3. mldm has no active-depth filtering.
        4. KE normalization by total energy produces a bounded signal.
        """
        our = self._get_momentum_history(len(mldm_signals))
        n = min(len(our), len(mldm_signals))
        if n < 20:
            return {
                "correlation": 0.0,
                "n_samples": n,
                "sufficient_data": False,
            }

        theirs = np.array(mldm_signals[:n], dtype=np.float64)
        our_slice = our[:n]

        our_std = float(our_slice.std())
        theirs_std = float(theirs.std())
        if our_std < _EPSILON or theirs_std < _EPSILON:
            return {
                "correlation": 0.0,
                "n_samples": n,
                "sufficient_data": True,
                "note": "one signal has near-zero variance",
            }

        corr = float(np.corrcoef(our_slice, theirs)[0, 1])
        return {
            "correlation": corr,
            "n_samples": n,
            "sufficient_data": True,
            "is_distinct": abs(corr) < 0.7,
            "theoretical_distinction": (
                "lob_kinetic_energy uses q*v^2 (energy) and q*v (momentum) "
                "aggregation with active-depth filtering and BBO-shift guard.  "
                "mldm_depth_momentum uses linear delta_q sums per level.  The "
                "quadratic velocity weighting amplifies large, fast changes while "
                "suppressing small oscillations that dominate linear measures.  "
                "skip_l1 option further reduces OFI-family correlation."
            ),
        }


ALPHA_CLASS = LobKineticEnergyAlpha

__all__ = ["LobKineticEnergyAlpha", "ALPHA_CLASS"]
