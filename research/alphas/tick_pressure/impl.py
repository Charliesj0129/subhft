"""Tick Pressure Alpha — directional tick weighted by L1 queue size.

Signal:  TP_t = EMA_8( sign(mid_t - mid_{t-1}) × (V_bid + V_ask) / max(EMA_64(V_bid + V_ask), 1) )

A positive TP → upward tick with large L1 queue → strong buying pressure.
A negative TP → downward tick with large L1 queue → strong selling pressure.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ≈ 8 ticks → α = 1 − exp(−1/8) ≈ 0.1175
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)
# EMA decay: window ≈ 64 ticks → α = 1 − exp(−1/64) ≈ 0.01550
_EMA_ALPHA_64: float = 1.0 - math.exp(-1.0 / 64.0)

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="tick_pressure",
    hypothesis=(
        "Price tick direction weighted by L1 queue size relative to baseline:"
        " large queue + upward tick = strong buying pressure,"
        " large queue + downward tick = strong selling pressure."
    ),
    formula="TP_t = EMA_8( sign(mid_t - mid_{t-1}) × (V_bid + V_ask) / max(EMA_64(V_bid + V_ask), 1) )",
    paper_refs=(),
    data_fields=("mid_price_x2", "l1_bid_qty", "l1_ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class TickPressureAlpha:
    """O(1) tick-pressure predictor with dual EMA smoothing.

    update() accepts either:
      - 3 positional args:  mid_price_x2, l1_bid_qty, l1_ask_qty
      - keyword args:       mid_price_x2=..., l1_bid_qty=..., l1_ask_qty=...
    """

    __slots__ = ("_prev_mid", "_queue_ema64", "_pressure_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._prev_mid: float = 0.0
        self._queue_ema64: float = 0.0
        self._pressure_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Ingest one tick and return the updated signal."""
        # --- resolve mid_price_x2, l1_bid_qty, l1_ask_qty ---
        if len(args) >= 3:
            mid = float(args[0])
            bid_qty = float(args[1])
            ask_qty = float(args[2])
        elif len(args) == 1 or len(args) == 2:
            raise ValueError(
                "update() requires 3 positional args (mid_price_x2, l1_bid_qty, l1_ask_qty)"
                " or keyword args"
            )
        else:
            mid = float(kwargs.get("mid_price_x2", 0.0))
            bid_qty = float(kwargs.get("l1_bid_qty", 0.0))
            ask_qty = float(kwargs.get("l1_ask_qty", 0.0))

        total_qty = bid_qty + ask_qty

        if not self._initialized:
            # First tick: store prev_mid, init queue_ema, return 0.
            self._prev_mid = mid
            self._queue_ema64 = total_qty
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # --- Compute sign of mid-price change ---
        delta = mid - self._prev_mid
        if delta > 0.0:
            sign = 1.0
        elif delta < 0.0:
            sign = -1.0
        else:
            sign = 0.0

        # --- Update queue baseline EMA(64) ---
        self._queue_ema64 += _EMA_ALPHA_64 * (total_qty - self._queue_ema64)

        # --- Compute raw pressure: sign × queue_ratio ---
        baseline = max(self._queue_ema64, 1.0)
        raw_pressure = sign * (total_qty / baseline)

        # --- Update pressure EMA(8) ---
        self._pressure_ema += _EMA_ALPHA_8 * (raw_pressure - self._pressure_ema)

        self._signal = self._pressure_ema
        self._prev_mid = mid
        return self._signal

    def reset(self) -> None:
        self._prev_mid = 0.0
        self._queue_ema64 = 0.0
        self._pressure_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = TickPressureAlpha

__all__ = ["TickPressureAlpha", "ALPHA_CLASS"]
