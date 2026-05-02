"""Intensity Burst Signal — tick arrival rate surge detector.

Signal: binary indicator (1 = burst active, 0 = normal) based on whether
current tick arrival rate exceeds multiplier * rolling baseline rate.

Paper ref:
  Christensen (2024) — intensity-based microstructure regime detection.

Allocator Law  : __slots__ on class; no heap allocations in update().
Precision Law  : all timestamps int (nanoseconds); signal is int {0, 1}.
Cache Law      : delegates to BurstDetector (pre-allocated ring buffer).
"""

from __future__ import annotations

from hft_platform.feature.burst_detector import BurstDetector
from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

_MANIFEST = AlphaManifest(
    alpha_id="intensity_burst",
    hypothesis=(
        "Abnormal tick arrival rate surges (>3x rolling median) signal "
        "regime shifts — potential news, large order activity, or liquidity "
        "events.  Burst state predicts elevated short-term volatility."
    ),
    formula=(
        "EMA baseline of tick count per 30s window; "
        "burst = current_count > 3 * baseline_count; "
        "signal = int(is_burst)"
    ),
    paper_refs=("Christensen2024",),
    data_fields=("tick_timestamps",),
    complexity="O(capacity) per tick (ring buffer scan, capacity-bounded at 512)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version=None,  # uses raw tick timestamps, not FeatureEngine
)


class IntensityBurstAlpha:
    """Tick intensity burst regime detector.

    Wraps BurstDetector for use in the alpha research pipeline.

    Parameters
    ----------
    window_ns : int
        Rolling window for tick counting. Default: 30_000_000_000 (30s).
    multiplier : float
        Burst threshold multiplier. Default: 3.0.
    cooldown_ns : int
        Minimum time between burst signals. Default: 5_000_000_000 (5s).
    """

    __slots__ = ("_detector", "_signal")

    def __init__(
        self,
        window_ns: int = 30_000_000_000,
        multiplier: float = 3.0,
        cooldown_ns: int = 5_000_000_000,
    ) -> None:
        self._detector = BurstDetector(
            window_ns=window_ns,
            multiplier=multiplier,
            cooldown_ns=cooldown_ns,
            capacity=512,
            enabled=True,
        )
        self._signal: int = 0

    def update(self, ts_ns: int) -> int:
        """Process a tick and return current signal.

        Parameters
        ----------
        ts_ns : int
            Tick timestamp in nanoseconds.

        Returns
        -------
        int
            1 if burst active, 0 otherwise.
        """
        self._detector.on_tick(ts_ns)
        self._signal = 1 if self._detector.is_burst else 0
        return self._signal

    @property
    def signal(self) -> int:
        """Current signal value: 1 = burst, 0 = normal."""
        return self._signal

    @property
    def tick_rate(self) -> int:
        """Current tick rate in milliticks/s."""
        return self._detector.tick_rate

    @property
    def baseline_rate(self) -> int:
        """Baseline tick rate in milliticks/s."""
        return self._detector.baseline_rate

    @property
    def is_burst(self) -> bool:
        """Whether currently in burst state."""
        return self._detector.is_burst

    def reset(self) -> None:
        """Reset all internal state."""
        self._detector.reset()
        self._signal = 0
