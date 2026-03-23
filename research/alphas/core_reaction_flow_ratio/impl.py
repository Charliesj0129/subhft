"""Core/Reaction Flow Ratio Alpha — Hawkes branching ratio asymmetry.

Signal:  directional = n_sell - n_buy  (linear, not absolute)
Where:
    n_side = max(0, 1 - sqrt(2 / (m2_side + 1)))   [Bacry et al. 2015]
    m2_side = E[tau^2] / (E[tau])^2   (normalized second moment of inter-arrivals)

Decomposes order flow into autonomous "core" (informed) vs reactive "noise"
using per-side Hawkes branching ratio estimated from inter-arrival time moments.
A side with LOW branching ratio has more autonomous/core flow (informed traders).
A side with HIGH branching ratio has more reactive flow (noise/MM).

Branching ratio formula: n = 1 - sqrt(2/(m2+1)) per Bacry, Mastromatteo,
Muzy (2015).  Less biased than n = 1 - 1/sqrt(m2): for Poisson (m2=2),
Bacry gives n ~ 0.184 vs naive 0.293.

Flat-tick handling: Lee-Ready convention — flat ticks assigned to last known side.
Accumulator drift: full recomputation every _RECOMPUTE_INTERVAL evictions.

Paper refs:
  Muhle-Karbe, Rosenbaum et al. (2026) arXiv:2601.23172
  Filimonov, Sornette (2012) — Hawkes endogeneity ratio.
  Bacry, Mastromatteo, Muzy (2015) — moment-based Hawkes estimation.

Allocator Law  : __slots__ on class; pre-allocated ring buffers.
Precision Law  : output is float (signal score, not price).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""

from __future__ import annotations

import math

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

_RING_SIZE: int = 1000
_MIN_OBSERVATIONS: int = 30
_EMA_OUTPUT: float = 1.0 - math.exp(-1.0 / 8.0)
_SIGNAL_CLIP: float = 2.0
_EPSILON: float = 1e-12
_RECOMPUTE_INTERVAL: int = 1000

_MANIFEST = AlphaManifest(
    alpha_id="core_reaction_flow_ratio",
    hypothesis=(
        "Asymmetric Hawkes branching ratios between buy and sell sides "
        "detect informed flow: the side with lower branching ratio (more "
        "autonomous/core orders) carries informed trading."
    ),
    formula=(
        "n_side = max(0, 1 - sqrt(2/(m2+1))), m2 = E[tau^2]/(E[tau])^2; "
        "signal = EMA_8(n_sell - n_buy), clipped [-2, 2]"
    ),
    paper_refs=("arXiv:2601.23172", "Filimonov-Sornette 2012", "Bacry-Mastromatteo-Muzy 2015"),
    data_fields=("timestamp_ns", "price", "volume"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class _InterArrivalRing:
    __slots__ = (
        "_buf", "_head", "_count", "_capacity",
        "_sum_tau", "_sum_tau_sq", "_last_ts", "_has_last", "_eviction_counter",
    )

    def __init__(self, capacity: int = _RING_SIZE) -> None:
        self._buf: np.ndarray = np.zeros(capacity, dtype=np.float64)
        self._head: int = 0
        self._count: int = 0
        self._capacity: int = capacity
        self._sum_tau: float = 0.0
        self._sum_tau_sq: float = 0.0
        self._last_ts: int = 0
        self._has_last: bool = False
        self._eviction_counter: int = 0

    def add(self, timestamp_ns: int) -> None:
        if not self._has_last:
            self._last_ts = timestamp_ns
            self._has_last = True
            return
        tau = float(timestamp_ns - self._last_ts)
        self._last_ts = timestamp_ns
        if tau <= 0.0:
            return
        if self._count >= self._capacity:
            old_tau = self._buf[self._head]
            self._sum_tau -= old_tau
            self._sum_tau_sq -= old_tau * old_tau
            self._eviction_counter += 1
            if self._eviction_counter >= _RECOMPUTE_INTERVAL:
                self._recompute_sums()
                self._eviction_counter = 0
        else:
            self._count += 1
        self._buf[self._head] = tau
        self._sum_tau += tau
        self._sum_tau_sq += tau * tau
        self._head = (self._head + 1) % self._capacity

    def _recompute_sums(self) -> None:
        if self._count == 0:
            self._sum_tau = 0.0
            self._sum_tau_sq = 0.0
            return
        valid = self._buf if self._count >= self._capacity else self._buf[:self._count]
        self._sum_tau = float(np.sum(valid))
        self._sum_tau_sq = float(np.dot(valid, valid))

    def branching_ratio(self) -> float:
        if self._count < _MIN_OBSERVATIONS:
            return 0.0
        mean_tau = self._sum_tau / self._count
        if mean_tau < _EPSILON:
            return 0.0
        mean_tau_sq = self._sum_tau_sq / self._count
        m2 = mean_tau_sq / (mean_tau * mean_tau)
        ratio = 2.0 / (m2 + 1.0)
        if ratio >= 1.0:
            return 0.0
        return max(0.0, 1.0 - math.sqrt(ratio))

    @property
    def count(self) -> int:
        return self._count

    def reset(self) -> None:
        self._buf[:] = 0.0
        self._head = 0
        self._count = 0
        self._sum_tau = 0.0
        self._sum_tau_sq = 0.0
        self._last_ts = 0
        self._has_last = False
        self._eviction_counter = 0


class CoreReactionFlowRatioAlpha:
    __slots__ = (
        "_buy_ring", "_sell_ring", "_prev_price", "_last_side",
        "_output_ema", "_signal", "_initialized", "_tick_count",
    )

    def __init__(self) -> None:
        self._buy_ring: _InterArrivalRing = _InterArrivalRing(_RING_SIZE)
        self._sell_ring: _InterArrivalRing = _InterArrivalRing(_RING_SIZE)
        self._prev_price: float = 0.0
        self._last_side: int = 0
        self._output_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False
        self._tick_count: int = 0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: C901
        if len(args) >= 3:
            timestamp_ns = int(args[0])
            price = float(args[1])
        elif len(args) == 2:
            timestamp_ns = int(args[0])
            price = float(args[1])
        elif args:
            raise ValueError("update() requires at least 2 positional args (timestamp_ns, price)")
        else:
            timestamp_ns = int(kwargs.get("timestamp_ns", 0))
            price = float(kwargs.get("price", 0.0))

        self._tick_count += 1
        if not self._initialized:
            self._prev_price = price
            self._initialized = True
            self._signal = 0.0
            return self._signal

        delta_price = price - self._prev_price
        self._prev_price = price

        if delta_price > 0:
            side = 1
        elif delta_price < 0:
            side = -1
        else:
            side = self._last_side

        if side == 1:
            self._buy_ring.add(timestamp_ns)
            self._last_side = 1
        elif side == -1:
            self._sell_ring.add(timestamp_ns)
            self._last_side = -1

        n_buy = self._buy_ring.branching_ratio()
        n_sell = self._sell_ring.branching_ratio()
        raw = n_sell - n_buy

        self._output_ema += _EMA_OUTPUT * (raw - self._output_ema)
        self._signal = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, self._output_ema))
        return self._signal

    def get_branching_ratios(self) -> tuple[float, float]:
        return self._buy_ring.branching_ratio(), self._sell_ring.branching_ratio()

    def get_asymmetry(self) -> float:
        n_buy, n_sell = self.get_branching_ratios()
        return abs(n_buy - n_sell)

    def reset(self) -> None:
        self._buy_ring.reset()
        self._sell_ring.reset()
        self._prev_price = 0.0
        self._last_side = 0
        self._output_ema = 0.0
        self._signal = 0.0
        self._initialized = False
        self._tick_count = 0

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = CoreReactionFlowRatioAlpha

__all__ = ["CoreReactionFlowRatioAlpha", "ALPHA_CLASS"]
