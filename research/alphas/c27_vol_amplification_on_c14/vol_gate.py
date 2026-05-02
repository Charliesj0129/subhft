"""Within-day 1-minute realized-vol percentile tracker.

Design (per R14-T1 §2.1):
  - Bucket ticks by minute-of-session. For each 1-min bucket, compute
    realized vol = std of tick-to-tick mid returns within that minute.
  - Maintain a running histogram of completed 1-minute vols within the
    CURRENT DAY. Reset at day boundary and on gap events.
  - At any time, answer: is the *current* (in-progress) minute's
    running vol above the P-th percentile of today's completed minutes?

Hysteresis (per R14-T1 §1):
  - Trigger AMPLIFY when percentile > P_high (default 0.90).
  - Release AMPLIFY when percentile < P_low (default 0.70).
  - Between P_low and P_high: hold previous state.

Notes:
  - Until the day has ≥``warmup_minutes`` completed buckets, the gate
    returns ``False`` (insufficient sample for percentile ranking).
  - Not thread-safe. Hot path usage is single-threaded.
  - Research-module float exception applies (rule 11). Internally uses
    float arithmetic for running sums; output flag is boolean.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class _MinuteAccumulator:
    """Running state for the minute currently being accumulated."""

    minute_id: int = -1  # session-minute key, -1 = not yet seeded
    last_mid: float = 0.0
    n_returns: int = 0
    sum_ret: float = 0.0
    sum_ret_sq: float = 0.0

    def reset_for_new_minute(self, minute_id: int, mid: float) -> None:
        self.minute_id = minute_id
        self.last_mid = mid
        self.n_returns = 0
        self.sum_ret = 0.0
        self.sum_ret_sq = 0.0

    def observe(self, mid: float) -> None:
        if self.last_mid > 0.0 and mid > 0.0:
            r = mid - self.last_mid  # in raw scaled units; magnitude-only
            self.n_returns += 1
            self.sum_ret += r
            self.sum_ret_sq += r * r
        self.last_mid = mid

    def running_std(self) -> float:
        """Unbiased std of within-minute returns so far. 0.0 if n<2."""
        if self.n_returns < 2:
            return 0.0
        mean = self.sum_ret / self.n_returns
        var = self.sum_ret_sq / self.n_returns - mean * mean
        if var <= 0.0:
            return 0.0
        return math.sqrt(var)


@dataclass
class VolPercentileGate:
    """Tracks within-day 1-min realized-vol percentile and a hysteresis state.

    Parameters
    ----------
    threshold_high : float
        Amplify trigger percentile. Default 0.90 — fire on top-10% minutes.
    threshold_low : float
        Amplify release percentile. Default 0.70 — release on decline.
    warmup_minutes : int
        Minimum completed minutes before gate can return True. Default 10.

    Hysteresis + warmup together prevent noisy early-session triggers.
    """

    threshold_high: float = 0.90
    threshold_low: float = 0.70
    warmup_minutes: int = 10
    _day_key: int = -1
    _completed_vols: list[float] = field(default_factory=list)
    _current: _MinuteAccumulator = field(default_factory=_MinuteAccumulator)
    _amplified: bool = False
    _updates: int = 0

    @property
    def amplified(self) -> bool:
        return self._amplified

    @property
    def completed_minutes(self) -> int:
        return len(self._completed_vols)

    @property
    def day_key(self) -> int:
        return self._day_key

    @property
    def updates(self) -> int:
        return self._updates

    def reset(self) -> None:
        """Hard reset — all state cleared. Use on day boundary or gap."""
        self._day_key = -1
        self._completed_vols.clear()
        self._current = _MinuteAccumulator()
        self._amplified = False

    def update(self, ts_ns: int, mid: float) -> bool:
        """Observe a tick. Returns the amplified flag AFTER this update.

        State decisions happen ONLY on minute boundaries to avoid within-
        minute thrashing: when a minute closes, rank its realized vol
        against the day's prior completed minutes and update the gate.
        The resulting state is held for the ENTIRE next minute.

        Parameters
        ----------
        ts_ns : int
            Event timestamp (nanoseconds since epoch OR monotonic).
        mid : float
            Mid-price at this tick (any scale — only return magnitudes
            within a minute matter for realized-vol std).
        """
        self._updates += 1
        day_key = _day_from_ts_ns(ts_ns)
        if day_key != self._day_key:
            # Day boundary → reset history (per design).
            self._day_key = day_key
            self._completed_vols.clear()
            self._current = _MinuteAccumulator()
            self._amplified = False

        minute_id = _minute_from_ts_ns(ts_ns)
        if minute_id != self._current.minute_id:
            # Close prior minute — append to history and re-evaluate state.
            prior_std = self._current.running_std()
            if self._current.minute_id >= 0 and self._current.n_returns >= 2:
                # Evaluate percentile of the just-CLOSED minute against
                # the history BEFORE it (not including itself), then
                # append.
                if len(self._completed_vols) >= self.warmup_minutes:
                    pct = self._percentile_rank(prior_std)
                    # Hysteresis: switch at HIGH, release at LOW, hold otherwise.
                    if pct >= self.threshold_high:
                        self._amplified = True
                    elif pct < self.threshold_low:
                        self._amplified = False
                self._completed_vols.append(prior_std)
            self._current.reset_for_new_minute(minute_id, mid)
        else:
            self._current.observe(mid)

        return self._amplified

    def _percentile_rank(self, x: float) -> float:
        """Fractional percentile rank (canonical form).

        rank(x) = (count(v < x) + 0.5 * count(v == x)) / n

        With this definition, a value in a tied cluster gets mid-cluster
        rank (not top-of-cluster), so a low-vol minute in a history
        dominated by low-vol minutes gets a middle-ish rank rather than
        the rank of the highest tied value.
        """
        n = len(self._completed_vols)
        if n == 0:
            return 0.0
        below = sum(1 for v in self._completed_vols if v < x)
        equal = sum(1 for v in self._completed_vols if v == x)
        return (below + 0.5 * equal) / n


def _day_from_ts_ns(ts_ns: int) -> int:
    """Convert ts_ns to a date ordinal.

    Accepts both monotonic-ns (test) and epoch-ns (CK) inputs. Uses a
    constant 86_400 * 1_000_000_000 = NS_PER_DAY divisor; negative or
    near-zero test timestamps still get a stable integer key.
    """
    return ts_ns // 86_400_000_000_000


def _minute_from_ts_ns(ts_ns: int) -> int:
    """Integer minute-of-epoch key."""
    return ts_ns // 60_000_000_000
