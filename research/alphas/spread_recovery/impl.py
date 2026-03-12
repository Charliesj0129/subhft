"""Spread Recovery Alpha — measures spread recovery speed after widening.

Signal:  spread_dev = spread_scaled - _spread_ema32
         _peak_dev  = max(_peak_dev * DECAY, abs(spread_dev))
         delta_spread = spread_scaled - _prev_spread
         recovery_raw = -delta_spread / max(_peak_dev, 1)
         signal = EMA_16(recovery_raw)

Hypothesis: Speed of spread recovery after widening indicates market resilience.
Fast recovery (positive signal after widening) = healthy liquidity.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : spread_scaled is int (x10000); output is float (signal score).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay constants
_EMA_ALPHA_16: float = 1.0 - math.exp(-1.0 / 16.0)
_EMA_ALPHA_32: float = 1.0 - math.exp(-1.0 / 32.0)
_PEAK_DECAY: float = 0.99

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="spread_recovery",
    hypothesis=(
        "Speed of spread recovery after widening indicates market resilience."
        " Fast recovery (positive signal after widening) = healthy liquidity."
    ),
    formula=("recovery_raw = -delta_spread / max(peak_dev, 1); signal = EMA_16(recovery_raw)"),
    paper_refs=(),
    data_fields=("spread_scaled",),
    complexity="O(1)",
    status=AlphaStatus.GATE_B,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class SpreadRecoveryAlpha:
    """O(1) spread-recovery signal with EMA smoothing.

    update() accepts either:
      - 1 positional arg:  spread_scaled
      - keyword arg:       spread_scaled=...
    """

    __slots__ = (
        "_spread_ema32",
        "_prev_spread",
        "_peak_dev",
        "_recovery_ema",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        self._spread_ema32: float = 0.0
        self._prev_spread: float = 0.0
        self._peak_dev: float = 0.0
        self._recovery_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Ingest one tick of spread_scaled and return updated signal."""
        # --- resolve spread_scaled ---
        if args:
            spread_scaled = float(args[0])
        else:
            spread_scaled = float(kwargs.get("spread_scaled", 0.0))

        if not self._initialized:
            self._spread_ema32 = spread_scaled
            self._prev_spread = spread_scaled
            self._peak_dev = 0.0
            self._recovery_ema = 0.0
            self._signal = 0.0
            self._initialized = True
            return self._signal

        # Deviation from EMA-32 baseline
        spread_dev = spread_scaled - self._spread_ema32

        # Decaying peak tracker
        self._peak_dev = max(self._peak_dev * _PEAK_DECAY, abs(spread_dev))

        # Spread change
        delta_spread = spread_scaled - self._prev_spread

        # Recovery raw: negative delta (narrowing) normalized by peak
        peak_floor = max(self._peak_dev, 1.0)
        recovery_raw = -delta_spread / peak_floor

        # EMA-16 smoothing of recovery signal
        self._recovery_ema += _EMA_ALPHA_16 * (recovery_raw - self._recovery_ema)
        self._signal = self._recovery_ema

        # Update trailing state
        self._spread_ema32 += _EMA_ALPHA_32 * (spread_scaled - self._spread_ema32)
        self._prev_spread = spread_scaled

        return self._signal

    def reset(self) -> None:
        self._spread_ema32 = 0.0
        self._prev_spread = 0.0
        self._peak_dev = 0.0
        self._recovery_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = SpreadRecoveryAlpha

__all__ = ["SpreadRecoveryAlpha", "ALPHA_CLASS"]
