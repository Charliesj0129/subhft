"""Spread-Volume Cross Alpha — cross-feature information incorporation signal.

Signal:  SVC_t = EMA_8( -delta_spread_bps * activity_surprise * sign(imbalance) )

Where:
  delta_spread_bps  = spread_bps_t - spread_bps_{t-1}
  activity          = volume if volume > 0 else |delta_bid_qty| + |delta_ask_qty|
  activity_surprise = activity / EMA(activity)  (ratio > 1 = spike)
  sign(imbalance)   = sign(bid_qty - ask_qty)

When trade volume is available, it is used directly.  On L1-only data
(volume=0), queue-change magnitude serves as an activity proxy so the
signal remains meaningful.

Hypothesis: When spread narrows AND activity spikes simultaneously,
information is being incorporated into price.  The cross-product of spread
compression and activity burst, signed by imbalance, predicts direction.

Positive signal = informed buying; negative = informed selling.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price -- no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)
_EPSILON: float = 1e-8  # guards against division by zero

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="spread_volume_cross",
    hypothesis=(
        "When spread narrows AND volume spikes simultaneously, information is"
        " being incorporated into price. The cross-product of spread compression"
        " and volume burst, signed by imbalance, predicts direction."
    ),
    formula="SVC_t = EMA_8(-delta_spread_bps * activity_surprise * sign(imbalance))",
    paper_refs=(),
    data_fields=("spread_bps", "volume", "bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


def _sign(x: float) -> float:
    """Return -1.0, 0.0, or 1.0."""
    if x > 0.0:
        return 1.0
    if x < 0.0:
        return -1.0
    return 0.0


class SpreadVolumeCrossAlpha:
    """O(1) spread-volume cross predictor with EMA smoothing.

    update() accepts keyword args: spread_bps, volume, bid_qty, ask_qty.
    """

    __slots__ = (
        "_prev_spread",
        "_prev_bid_qty",
        "_prev_ask_qty",
        "_activity_ema",
        "_signal_ema",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        self._prev_spread: float = 0.0
        self._prev_bid_qty: float = 0.0
        self._prev_ask_qty: float = 0.0
        self._activity_ema: float = 0.0
        self._signal_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: ANN002
        """Update signal with new spread_bps, volume, bid_qty, ask_qty values."""
        spread_bps = float(kwargs.get("spread_bps", 0.0))
        volume = float(kwargs.get("volume", 0.0))
        bid_qty = float(kwargs.get("bid_qty", 0.0))
        ask_qty = float(kwargs.get("ask_qty", 0.0))

        if not self._initialized:
            # First tick: initialize baselines, return 0.0 (no delta yet).
            self._prev_spread = spread_bps
            self._prev_bid_qty = bid_qty
            self._prev_ask_qty = ask_qty
            self._activity_ema = max(volume, _EPSILON)
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # delta_spread: negative means spread narrowed (information event).
        delta_spread = spread_bps - self._prev_spread
        self._prev_spread = spread_bps

        # Activity: use trade volume when available; otherwise fall back to
        # queue-change magnitude (|delta_bid_qty| + |delta_ask_qty|) so the
        # signal works on L1-only data where volume is always zero.
        if volume > 0.0:
            activity = volume
        else:
            activity = abs(bid_qty - self._prev_bid_qty) + abs(ask_qty - self._prev_ask_qty)
        self._prev_bid_qty = bid_qty
        self._prev_ask_qty = ask_qty

        # Activity surprise: ratio of current activity to EMA baseline.
        self._activity_ema += _EMA_ALPHA * (activity - self._activity_ema)
        activity_surprise = activity / max(self._activity_ema, _EPSILON)

        # Imbalance direction.
        imbalance_sign = _sign(bid_qty - ask_qty)

        # Raw cross signal: negative delta_spread (narrowing) * activity spike * direction.
        raw = -delta_spread * activity_surprise * imbalance_sign

        # EMA smoothing of signal.
        self._signal_ema += _EMA_ALPHA * (raw - self._signal_ema)
        self._signal = self._signal_ema
        return self._signal

    def reset(self) -> None:
        self._prev_spread = 0.0
        self._prev_bid_qty = 0.0
        self._prev_ask_qty = 0.0
        self._activity_ema = 0.0
        self._signal_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = SpreadVolumeCrossAlpha

__all__ = ["SpreadVolumeCrossAlpha", "ALPHA_CLASS"]
