"""Drift-Burst Detector — real-time microstructure toxicity scoring for StormGuard.

Implements the Christensen, Oomen, Reno (2022) Drift-Burst Hypothesis test adapted
for tick-by-tick LOB data. Detects bursts of informed trading where the local drift
significantly exceeds what is expected under a martingale null.

Test statistic: T(t) = drift_estimate / sqrt(bpv_estimate)
Where:
  drift_estimate = rolling mean of log-returns over short window
  bpv_estimate   = bipower variation (robust volatility, avoids jumps)

Burst detection: |T(t)| > threshold (calibrated from standard normal asymptotic null).

Integration: StormGuard calls `evaluate(mid_price_x2, spread_scaled, imbalance)`
and receives `(burst_detected, toxicity_score)` where toxicity_score in [0, 1].

Paper refs:
  Christensen, Oomen, Reno (2022) — "The Drift Burst Hypothesis", Journal of
    Econometrics. doi:10.1016/j.jeconom.2021.11.004

Allocator Law  : All arrays pre-allocated; no heap allocation on update().
Precision Law  : mid_price_x2 is scaled int; returns/BPV are float (non-accounting).
Cache Law      : Ring buffers for returns, contiguous float64.
Async Law      : Pure computation, no IO.
"""

from __future__ import annotations

import math
from typing import NamedTuple

import numpy as np
from structlog import get_logger

logger = get_logger("risk.drift_burst")


_EPS: float = 1e-15
_DEFAULT_WINDOW_SIZE: int = 100
_DEFAULT_BURST_THRESHOLD: float = 3.0
_DEFAULT_COOLDOWN_TICKS: int = 50
_DEFAULT_COOLDOWN_NS: int = 5_000_000_000
_DEFAULT_MIN_BPV: float = 1e-10
_MAX_WINDOW: int = 2048
_MU1_INV_SQ: float = math.pi / 2.0


class BurstEvent(NamedTuple):
    """Immutable burst detection event."""

    ts: int
    direction: int
    magnitude: float
    toxicity_type: str
    t_statistic: float


class ToxicityResult(NamedTuple):
    """Result from DriftBurstDetector.evaluate()."""

    burst_detected: bool
    toxicity_score: float
    burst_event: BurstEvent | None


class DriftBurstDetector:
    """Real-time drift-burst detection for StormGuard integration.

    Computes a rolling T-statistic that tests whether local price drift exceeds
    what is expected under diffusion-only dynamics. The bipower variation (BPV)
    provides a jump-robust volatility estimate for the denominator.

    Thread safety: NOT thread-safe. Designed for single-threaded event loop.
    """

    __slots__ = (
        "_window_size",
        "_burst_threshold",
        "_cooldown_ticks",
        "_cooldown_ns",
        "_min_bpv",
        "_skip_zero_returns",
        "_returns",
        "_abs_returns",
        "_head",
        "_count",
        "_last_mid_x2",
        "_last_update_ns",
        "_stale_reset_ns",
        "_ticks_since_burst",
        "_last_burst_ts",
        "_in_cooldown",
        "_t_statistic",
        "_toxicity_score",
        "_last_burst",
        "_drift_sum",
        "_symbol",
    )

    def __init__(
        self,
        window_size: int = _DEFAULT_WINDOW_SIZE,
        burst_threshold: float = _DEFAULT_BURST_THRESHOLD,
        cooldown_ticks: int = _DEFAULT_COOLDOWN_TICKS,
        cooldown_ns: int = _DEFAULT_COOLDOWN_NS,
        min_bpv: float = _DEFAULT_MIN_BPV,
        skip_zero_returns: bool = True,
        stale_reset_ns: int = 300_000_000_000,
    ) -> None:
        if window_size < 10:
            raise ValueError(f"window_size must be >= 10, got {window_size}")
        if window_size > _MAX_WINDOW:
            raise ValueError(f"window_size must be <= {_MAX_WINDOW}, got {window_size}")
        if burst_threshold <= 0.0:
            raise ValueError(f"burst_threshold must be > 0, got {burst_threshold}")

        self._window_size: int = window_size
        self._burst_threshold: float = burst_threshold
        self._cooldown_ticks: int = max(0, cooldown_ticks)
        self._cooldown_ns: int = max(0, cooldown_ns)
        self._min_bpv: float = max(0.0, min_bpv)
        self._skip_zero_returns: bool = skip_zero_returns

        self._returns: np.ndarray = np.zeros(window_size, dtype=np.float64)
        self._abs_returns: np.ndarray = np.zeros(window_size, dtype=np.float64)
        self._head: int = 0
        self._count: int = 0

        self._last_mid_x2: int = 0
        self._last_update_ns: int = 0
        self._stale_reset_ns: int = max(0, int(stale_reset_ns))
        self._ticks_since_burst: int = self._cooldown_ticks + 1
        self._last_burst_ts: int = 0
        self._in_cooldown: bool = False
        self._t_statistic: float = 0.0
        self._toxicity_score: float = 0.0
        self._last_burst: BurstEvent | None = None
        self._drift_sum: float = 0.0
        self._symbol: str = ""

    def evaluate(
        self,
        mid_price_x2: int,
        spread_scaled: int = 0,
        imbalance: float = 0.0,
        ts: int = 0,
        symbol: str = "",
    ) -> ToxicityResult:
        """Evaluate drift-burst on new LOB update.

        This is the primary StormGuard integration interface.

        Args:
            mid_price_x2: best_bid + best_ask (scaled int x10000).
            spread_scaled: best_ask - best_bid (scaled int x10000).
            imbalance: LOB imbalance ratio [-1, 1].
            ts: Timestamp in nanoseconds.
            symbol: Instrument this update belongs to. The ring buffer holds
                log-returns of ONE price series; feeding it a second instrument
                makes the t-statistic measure the gap between two contracts
                rather than drift in either. When the symbol changes the
                detector resets rather than mixing the series.

        Returns:
            ToxicityResult(burst_detected, toxicity_score, burst_event).
            toxicity_score is in [0, 1], suitable for StormGuard threshold comparison.
        """
        burst_event: BurstEvent | None = None

        if symbol and symbol != self._symbol:
            if self._symbol:
                self.reset()
            self._symbol = symbol

        if (
            self._stale_reset_ns > 0
            and ts > 0
            and self._last_update_ns > 0
            and (ts - self._last_update_ns) >= self._stale_reset_ns
        ):
            self.reset()
        if ts > 0:
            self._last_update_ns = ts

        self._ticks_since_burst += 1
        if self._in_cooldown:
            if self._cooldown_ns > 0 and ts > 0:
                if ts - self._last_burst_ts >= self._cooldown_ns:
                    self._in_cooldown = False
            elif self._ticks_since_burst > self._cooldown_ticks:
                self._in_cooldown = False

        if self._last_mid_x2 <= 0 or mid_price_x2 <= 0:
            self._last_mid_x2 = mid_price_x2
            return ToxicityResult(
                burst_detected=False,
                toxicity_score=0.0,
                burst_event=None,
            )

        log_ret = math.log(mid_price_x2 / self._last_mid_x2)
        self._last_mid_x2 = mid_price_x2

        if self._skip_zero_returns and log_ret == 0.0:
            return ToxicityResult(
                burst_detected=False,
                toxicity_score=self._toxicity_score,
                burst_event=None,
            )

        old_idx = self._head
        old_return = self._returns[old_idx]

        self._returns[old_idx] = log_ret
        self._abs_returns[old_idx] = abs(log_ret)
        self._head = (self._head + 1) % self._window_size

        if self._count < self._window_size:
            self._count += 1
            self._drift_sum += log_ret
            if self._count < self._window_size:
                self._toxicity_score = 0.0
                return ToxicityResult(
                    burst_detected=False,
                    toxicity_score=0.0,
                    burst_event=None,
                )
        else:
            self._drift_sum += log_ret - old_return

        n = self._count
        drift_estimate = self._drift_sum / n

        bpv = self._compute_bpv()

        if bpv < self._min_bpv:
            self._t_statistic = 0.0
            self._toxicity_score = 0.0
            return ToxicityResult(
                burst_detected=False,
                toxicity_score=0.0,
                burst_event=None,
            )

        vol_estimate = math.sqrt(bpv / n)
        self._t_statistic = drift_estimate / max(vol_estimate, _EPS)

        abs_t = abs(self._t_statistic)
        self._toxicity_score = 2.0 / (1.0 + math.exp(-abs_t / self._burst_threshold)) - 1.0

        burst_detected = False
        if abs_t > self._burst_threshold and not self._in_cooldown:
            burst_detected = True
            self._in_cooldown = True
            self._ticks_since_burst = 0
            self._last_burst_ts = ts

            direction = 1 if self._t_statistic > 0 else -1

            toxicity_type = self._classify_toxicity(
                direction=direction,
                spread_scaled=spread_scaled,
                imbalance=imbalance,
            )

            burst_event = BurstEvent(
                ts=ts,
                direction=direction,
                magnitude=abs_t,
                toxicity_type=toxicity_type,
                t_statistic=self._t_statistic,
            )
            self._last_burst = burst_event

            logger.info(
                "drift_burst_detected",
                symbol=self._symbol,
                direction=direction,
                t_stat=f"{self._t_statistic:.3f}",
                magnitude=f"{abs_t:.3f}",
                toxicity_type=toxicity_type,
                spread_scaled=spread_scaled,
                imbalance=f"{imbalance:.4f}",
            )

        return ToxicityResult(
            burst_detected=burst_detected,
            toxicity_score=self._toxicity_score,
            burst_event=burst_event,
        )

    def _compute_bpv(self) -> float:
        """Compute bipower variation over the current window.

        BPV = μ₁⁻² * (n/(n-1)) * (1/n) * Σ|r_i|·|r_{i-1}|
            = (π/2) * (1/(n-1)) * Σ|r_i|·|r_{i-1}|

        where μ₁⁻² = π/2 ≈ 1.5708 (Christensen, Oomen, Reno 2022).
        Uses contiguous array access for cache efficiency.
        """
        n = self._count
        if n < 2:
            return _EPS

        total = 0.0
        start = self._head
        for i in range(1, n):
            idx_curr = (start + i) % self._window_size
            idx_prev = (start + i - 1) % self._window_size
            total += self._abs_returns[idx_curr] * self._abs_returns[idx_prev]

        return _MU1_INV_SQ * total / (n - 1)

    @staticmethod
    def _classify_toxicity(
        direction: int,
        spread_scaled: int,
        imbalance: float,
    ) -> str:
        """Classify burst as informed or liquidity-driven.

        Heuristic: if spread is wide and imbalance aligns with burst direction,
        this is likely informed flow (market makers withdrawing). Otherwise,
        it may be a liquidity-driven transient.

        Args:
            direction: +1 (buy burst) or -1 (sell burst).
            spread_scaled: current spread in scaled int.
            imbalance: LOB imbalance [-1, 1].

        Returns:
            "informed" or "liquidity".
        """
        imbalance_opposes = (direction > 0 and imbalance < -0.2) or (direction < 0 and imbalance > 0.2)

        if imbalance_opposes and spread_scaled > 0:
            return "informed"

        return "liquidity"

    @property
    def t_statistic(self) -> float:
        """Current T-statistic value."""
        return self._t_statistic

    @property
    def toxicity_score(self) -> float:
        """Current toxicity score in [0, 1]."""
        return self._toxicity_score

    @property
    def is_warm(self) -> bool:
        """True when sufficient data has been collected."""
        return self._count >= self._window_size

    @property
    def last_burst(self) -> BurstEvent | None:
        """Most recent burst event, or None."""
        return self._last_burst

    def reset(self) -> None:
        """Reset all state for session boundary."""
        self._returns[:] = 0.0
        self._abs_returns[:] = 0.0
        self._head = 0
        self._count = 0
        self._last_mid_x2 = 0
        self._last_update_ns = 0
        self._ticks_since_burst = self._cooldown_ticks + 1
        self._last_burst_ts = 0
        self._in_cooldown = False
        self._t_statistic = 0.0
        self._toxicity_score = 0.0
        self._last_burst = None
        self._drift_sum = 0.0


__all__ = [
    "BurstEvent",
    "ToxicityResult",
    "DriftBurstDetector",
]
