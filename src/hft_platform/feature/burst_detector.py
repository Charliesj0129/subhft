"""Intensity Burst Detection — abnormal tick arrival rate surge detector.

Detects when tick density exceeds a configurable multiplier of the rolling
baseline rate, signaling potential news, large orders, or regime shifts.

Based on Christensen (2024) — intensity-based regime detection.

Reference parameters:
    TXFD6 median tick interval ~125ms (~8 ticks/s)
    TMFD6 median tick interval ~300ms (~3.3 ticks/s)

Allocator Law : __slots__, pre-allocated ring buffer, no heap in on_tick().
Cache Law     : contiguous list (not deque), circular index.
Precision Law : all timestamps int (nanoseconds), rates as int (milliticks/s).
"""

from __future__ import annotations

_DEFAULT_WINDOW_NS: int = 30_000_000_000  # 30 seconds
_DEFAULT_MULTIPLIER: float = 3.0
_DEFAULT_COOLDOWN_NS: int = 5_000_000_000  # 5 seconds
_DEFAULT_CAPACITY: int = 512
_EMA_ALPHA: float = 0.1  # slow EMA for baseline adaptation


class BurstDetector:
    """Detect abnormal tick density surges.

    Uses a pre-allocated circular buffer of tick timestamps to compute
    current tick rate, and an EMA of window tick counts as baseline.
    Flags burst when current count > multiplier * baseline count.

    Parameters
    ----------
    window_ns : int
        Rolling window for tick counting, in nanoseconds.
        Default: 30_000_000_000 (30s).
    multiplier : float
        Burst threshold multiplier over baseline. Default: 3.0.
    cooldown_ns : int
        Minimum time between burst signals, in nanoseconds.
        Default: 5_000_000_000 (5s).
    capacity : int
        Ring buffer capacity for tick timestamps.
        Default: 512 (sufficient for 30s at 8 ticks/s = 240 ticks).
    enabled : bool
        If False, never signals burst. Default: True.
    """

    __slots__ = (
        "_window_ns",
        "_multiplier",
        "_cooldown_ns",
        "_enabled",
        "_capacity",
        "_timestamps",
        "_head",
        "_count",
        "_baseline_count",
        "_is_burst",
        "_last_burst_ns",
        "_total_ticks",
        "_last_baseline_update_ns",
        "_first_tick_ns",
        "_warmed_up",
    )

    def __init__(
        self,
        window_ns: int = _DEFAULT_WINDOW_NS,
        multiplier: float = _DEFAULT_MULTIPLIER,
        cooldown_ns: int = _DEFAULT_COOLDOWN_NS,
        capacity: int = _DEFAULT_CAPACITY,
        enabled: bool = True,
    ) -> None:
        self._window_ns: int = window_ns
        self._multiplier: float = multiplier
        self._cooldown_ns: int = cooldown_ns
        self._enabled: bool = enabled
        self._capacity: int = capacity

        # Pre-allocated ring buffer of timestamps (nanoseconds).
        # Filled with 0 — sentinel value (no valid tick at t=0).
        self._timestamps: list[int] = [0] * capacity
        self._head: int = 0  # next write position
        self._count: int = 0  # number of valid entries in buffer

        # Baseline: EMA of tick count per window (starts at 0, warm-up needed).
        self._baseline_count: float = 0.0
        # First tick timestamp — used to detect warm-up completion.
        self._first_tick_ns: int = 0
        self._warmed_up: bool = False

        self._is_burst: bool = False
        self._last_burst_ns: int = 0
        self._total_ticks: int = 0
        self._last_baseline_update_ns: int = 0

    def on_tick(self, ts_ns: int) -> bool:
        """Register a tick arrival and evaluate burst condition.

        Parameters
        ----------
        ts_ns : int
            Tick timestamp in nanoseconds (monotonic).

        Returns
        -------
        bool
            True if a burst transition was detected on this tick
            (rising edge only, respecting cooldown).
        """
        if not self._enabled:
            return False

        # --- Insert tick into ring buffer (overwrite oldest if full) ---
        self._timestamps[self._head] = ts_ns
        self._head = (self._head + 1) % self._capacity
        if self._count < self._capacity:
            self._count += 1
        self._total_ticks += 1

        if self._total_ticks == 1:
            self._first_tick_ns = ts_ns

        # --- Count ticks within window ---
        cutoff: int = ts_ns - self._window_ns
        current_count: int = self._count_in_window(cutoff)

        # --- Warm-up: no detection until first full window has elapsed ---
        if not self._warmed_up:
            if ts_ns - self._first_tick_ns < self._window_ns:
                return False
            # First full window complete — seed baseline with actual count.
            self._baseline_count = float(current_count)
            self._last_baseline_update_ns = ts_ns
            self._warmed_up = True
            return False

        # --- Update baseline EMA at window-scale cadence ---
        # Critical: (1) updating per-tick causes baseline to chase burst rate,
        # preventing detection.  Update only once per window period.
        # (2) Never update baseline during burst — burst ticks would contaminate
        # the baseline upward, desensitizing future detection (Christensen 2024).
        if not self._is_burst and ts_ns - self._last_baseline_update_ns >= self._window_ns:
            self._baseline_count = _EMA_ALPHA * current_count + (1.0 - _EMA_ALPHA) * self._baseline_count
            self._last_baseline_update_ns = ts_ns

        # --- Burst detection ---
        threshold: float = self._multiplier * self._baseline_count
        was_burst: bool = self._is_burst

        if threshold > 0.0 and current_count > threshold:
            self._is_burst = True
        else:
            self._is_burst = False

        # Rising edge detection with cooldown.
        if self._is_burst and not was_burst:
            if ts_ns - self._last_burst_ns >= self._cooldown_ns:
                self._last_burst_ns = ts_ns
                return True

        return False

    def _count_in_window(self, cutoff: int) -> int:
        """Count timestamps strictly after cutoff in the ring buffer.

        O(n) where n = self._count, but n is bounded by capacity (512).
        This is acceptable for a per-tick call on a ~8 ticks/s instrument.
        """
        count: int = 0
        buf = self._timestamps
        n: int = self._count
        cap: int = self._capacity
        # Scan from tail (oldest) to head (newest).
        start: int = (self._head - n) % cap
        for i in range(n):
            idx: int = (start + i) % cap
            if buf[idx] > cutoff:
                count += 1
        return count

    @property
    def tick_rate(self) -> int:
        """Current tick rate as milliticks per second (int).

        Returns ticks_in_window * 1000 / window_seconds.
        Returns 0 if no ticks recorded.
        """
        if self._count == 0 or self._window_ns == 0:
            return 0
        # Use the most recent timestamp to compute actual window span.
        newest_idx: int = (self._head - 1) % self._capacity
        newest_ts: int = self._timestamps[newest_idx]
        cutoff: int = newest_ts - self._window_ns
        current_count: int = self._count_in_window(cutoff)
        # Rate = count / window_s = count * 1e9 / window_ns
        # milliticks/s = count * 1e12 / window_ns — but that overflows for large windows.
        # Use: milliticks_per_s = count * 1_000_000_000_000 // window_ns
        # For 30s window, 240 ticks: 240 * 1e12 / 30e9 = 8000 (8.0 ticks/s) ✓
        return int(current_count * 1_000_000_000_000 // self._window_ns)

    @property
    def baseline_rate(self) -> int:
        """Rolling baseline tick rate as milliticks per second (int).

        Returns baseline_count * 1000 / window_seconds.
        """
        if self._window_ns == 0:
            return 0
        return int(self._baseline_count * 1_000_000_000_000 // self._window_ns)

    @property
    def is_burst(self) -> bool:
        """Whether the detector is currently in burst state."""
        return self._is_burst

    @property
    def total_ticks(self) -> int:
        """Total number of ticks processed since construction or last reset."""
        return self._total_ticks

    def reset(self) -> None:
        """Clear all state, restoring to post-construction defaults."""
        for i in range(self._capacity):
            self._timestamps[i] = 0
        self._head = 0
        self._count = 0
        self._baseline_count = 0.0
        self._is_burst = False
        self._last_burst_ns = 0
        self._total_ticks = 0
        self._last_baseline_update_ns = 0
        self._first_tick_ns = 0
        self._warmed_up = False
