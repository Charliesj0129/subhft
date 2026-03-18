"""Trade Intensity Surprise Alpha — refs 1312.0514, 1809.08060.

Signal: Directional intensity surprise — LOB depth activity (a proxy for
trade arrival intensity) relative to its baseline, weighted by queue
imbalance direction.

Hypothesis (Lipton et al. 2013, Morariu-Patrichi & Pakkanen 2018):
  Trade arrival intensity is state-dependent: it varies with queue imbalance
  and spread. When depth activity accelerates beyond its baseline (measured
  by fast/slow EMA ratio of total depth changes), it signals directional
  pressure. The queue imbalance provides the direction.

  Depth activity = |Δbid_qty| + |Δask_qty| per tick. This proxies trade
  arrivals because LOBStatsEvent does not carry raw trade volume.

Formula:
  activity  = |bid_qty - prev_bid| + |ask_qty - prev_ask|
  act_fast += α8  * (activity - act_fast)
  act_slow += α64 * (activity - act_slow)
  qi        = (bid_qty - ask_qty) / (bid_qty + ask_qty + ε)
  qi_ema   += α8  * (qi - qi_ema)
  ir        = log(max(act_fast, ε) / max(act_slow, ε))
  signal    = qi_ema * ir

Regime interpretation:
  ir > 0 : depth activity acceleration → more informed flow
  ir < 0 : depth activity deceleration → lull
  qi > 0 : bid-dominant → upward pressure
  signal > 0 : accelerating + bid-dominant → strong buy signal

Allocator Law : __slots__, all state scalar, no heap in update().
Precision Law : float ok (signal score, not financial accounting).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# EMA coefficients
# ---------------------------------------------------------------------------
_A8: float = 1.0 - math.exp(-1.0 / 8.0)    # ≈ 0.1175
_A64: float = 1.0 - math.exp(-1.0 / 64.0)  # ≈ 0.0155

_EPSILON: float = 1e-8

# ---------------------------------------------------------------------------
# Manifest (pre-allocated, Allocator Law)
# ---------------------------------------------------------------------------
_MANIFEST = AlphaManifest(
    alpha_id="trade_intensity_surprise",
    hypothesis=(
        "Depth activity (|Δbid| + |Δask|) acceleration relative to baseline, "
        "weighted by queue imbalance direction, signals informed flow. "
        "High-intensity periods with directional imbalance predict price moves."
    ),
    formula=(
        "signal = EMA8(QI) * log(EMA8(activity) / EMA64(activity)); "
        "activity = |Δbid_qty| + |Δask_qty|"
    ),
    paper_refs=("1312.0514", "1809.08060"),
    data_fields=("bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "architect"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class TradeIntensitySurpriseAlpha:
    """O(1) trade intensity surprise predictor via depth activity.

    Five scalar EMA states + two prev-tick buffers:
      _act_fast   : fast activity EMA (window ≈ 8 ticks)
      _act_slow   : slow activity EMA (window ≈ 64 ticks)
      _qi_ema     : smoothed queue imbalance direction
      _prev_bid   : previous tick's bid_qty
      _prev_ask   : previous tick's ask_qty
      _signal     : cached output
      _tick_count : warmup counter
    """

    __slots__ = (
        "_act_fast", "_act_slow", "_qi_ema",
        "_prev_bid", "_prev_ask",
        "_signal", "_tick_count",
    )

    def __init__(self) -> None:
        self._act_fast: float = 0.0
        self._act_slow: float = 0.0
        self._qi_ema: float = 0.0
        self._prev_bid: float = 0.0
        self._prev_ask: float = 0.0
        self._signal: float = 0.0
        self._tick_count: int = 0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Update state with new tick data.

        Accepts:
          - 2+ positional args: bid_qty, ask_qty [, volume (ignored)]
          - keyword args: bid_qty=, ask_qty=
          - bids/asks arrays
        """
        if args and len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif "bid_qty" in kwargs and "ask_qty" in kwargs:
            bid_qty = float(kwargs["bid_qty"])
            ask_qty = float(kwargs["ask_qty"])
        elif "bids" in kwargs and "asks" in kwargs:
            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(bids[0][1]) if hasattr(bids, "__getitem__") and len(bids) > 0 else 0.0
            ask_qty = float(asks[0][1]) if hasattr(asks, "__getitem__") and len(asks) > 0 else 0.0
        else:
            bid_qty = 0.0
            ask_qty = 0.0

        self._tick_count += 1

        # Queue imbalance direction
        denom = bid_qty + ask_qty
        qi = (bid_qty - ask_qty) / (denom + _EPSILON)

        # Depth activity: total depth change magnitude (proxy for trade intensity)
        if self._tick_count == 1:
            # First tick: no delta available
            activity = 0.0
            self._qi_ema = qi
            self._act_fast = 0.0
            self._act_slow = 0.0
        else:
            activity = abs(bid_qty - self._prev_bid) + abs(ask_qty - self._prev_ask)
            self._act_fast += _A8 * (activity - self._act_fast)
            self._act_slow += _A64 * (activity - self._act_slow)
            self._qi_ema += _A8 * (qi - self._qi_ema)

        # Store for next delta
        self._prev_bid = bid_qty
        self._prev_ask = ask_qty

        # Intensity ratio: log of fast/slow activity ratio
        ratio = max(self._act_fast, _EPSILON) / max(self._act_slow, _EPSILON)
        ir = math.log(ratio)

        # Warmup guard: need 64+ ticks for slow EMA convergence
        if self._tick_count < 64:
            self._signal = 0.0
        else:
            self._signal = self._qi_ema * ir

        return self._signal

    def reset(self) -> None:
        """Clear all state."""
        self._act_fast = 0.0
        self._act_slow = 0.0
        self._qi_ema = 0.0
        self._prev_bid = 0.0
        self._prev_ask = 0.0
        self._signal = 0.0
        self._tick_count = 0

    def get_signal(self) -> float:
        """Return cached signal from last update()."""
        return self._signal


ALPHA_CLASS = TradeIntensitySurpriseAlpha

__all__ = ["TradeIntensitySurpriseAlpha", "ALPHA_CLASS"]
