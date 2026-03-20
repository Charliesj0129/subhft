"""Signal drift detection for live alpha monitoring.

Detects when a live signal's rolling mean has drifted significantly from its
backtest baseline distribution, using a z-score threshold test.

Float is permitted in alpha/ modules (see architecture rule 11).
"""

from __future__ import annotations

import collections
from dataclasses import dataclass

import structlog

_log = structlog.get_logger("alpha.drift_detector")
logger = _log


@dataclass(frozen=True, slots=True)
class DriftDetectorConfig:
    """Configuration for the drift detector.

    Attributes:
        window_size: Number of recent observations used for rolling mean.
        z_threshold: Z-score threshold above which drift is flagged.
        max_alerts: Maximum number of alerts the detector will emit before
            silencing further alerts (prevents alert storms).
    """

    window_size: int = 100
    z_threshold: float = 3.0
    max_alerts: int = 10


@dataclass(frozen=True, slots=True)
class DriftAlert:
    """Alert emitted when signal drift is detected.

    Attributes:
        z_score: Absolute z-score of rolling mean vs baseline.
        rolling_mean: Mean of the current rolling window.
        baseline_mean: Expected mean from backtest distribution.
        timestamp_ns: Nanosecond timestamp of the triggering observation.
    """

    z_score: float
    rolling_mean: float
    baseline_mean: float
    timestamp_ns: int


class DriftDetector:
    """Detects statistically significant drift in a live signal.

    Initialised from the signal's backtest distribution (mean + std).  On each
    new observation the detector computes a rolling mean over the last
    ``config.window_size`` values and compares it to the baseline using a
    z-score.  An alert is emitted when the z-score exceeds ``z_threshold``.

    Alert emission is capped at ``max_alerts`` to prevent alert storms during
    sustained drift episodes.

    Example::

        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0)
        alert = detector.observe(value=5.0, timestamp_ns=timebase.now_ns())
        if alert is not None:
            log.warning("drift detected", z_score=alert.z_score)
    """

    __slots__ = (
        "_baseline_mean",
        "_baseline_std",
        "_config",
        "_window",
        "_alerts",
        "_alert_count",
    )

    def __init__(
        self,
        baseline_mean: float,
        baseline_std: float,
        config: DriftDetectorConfig | None = None,
    ) -> None:
        """Initialise the detector from a backtest signal distribution.

        Args:
            baseline_mean: Expected mean of the signal under normal conditions.
            baseline_std: Expected standard deviation of the signal; must be
                positive for drift detection to be active.
            config: Optional configuration override.  Uses
                ``DriftDetectorConfig()`` defaults when not provided.
        """
        self._baseline_mean: float = baseline_mean
        self._baseline_std: float = baseline_std
        self._config: DriftDetectorConfig = config if config is not None else DriftDetectorConfig()
        self._window: collections.deque[float] = collections.deque(maxlen=self._config.window_size)
        self._alerts: list[DriftAlert] = []
        self._alert_count: int = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def observe(self, value: float, timestamp_ns: int = 0) -> DriftAlert | None:
        """Feed a new signal observation into the detector.

        Args:
            value: The latest signal value.
            timestamp_ns: Nanosecond timestamp of the observation.  Defaults
                to 0 when no timestamp source is available.

        Returns:
            A ``DriftAlert`` if drift is detected and the alert cap has not
            been reached; ``None`` otherwise.
        """
        self._window.append(value)

        # Window must be full before we make any inference.
        if len(self._window) < self._config.window_size:
            return None

        # Guard against degenerate baseline.
        if self._baseline_std <= 0.0:
            return None

        rolling_mean: float = sum(self._window) / len(self._window)
        z_score: float = abs(rolling_mean - self._baseline_mean) / self._baseline_std

        if z_score > self._config.z_threshold:
            if self._alert_count >= self._config.max_alerts:
                # Alert cap reached — silently absorb to avoid alert storms.
                return None

            alert = DriftAlert(
                z_score=z_score,
                rolling_mean=rolling_mean,
                baseline_mean=self._baseline_mean,
                timestamp_ns=timestamp_ns,
            )
            self._alerts.append(alert)
            self._alert_count += 1

            _log.warning(
                "signal drift detected",
                z_score=z_score,
                rolling_mean=rolling_mean,
                baseline_mean=self._baseline_mean,
                alert_count=self._alert_count,
            )
            return alert

        return None

    @property
    def alert_count(self) -> int:
        """Total number of drift alerts emitted so far."""
        return self._alert_count

    def reset(self) -> None:
        """Clear the rolling window and reset alert state.

        Useful when re-initialising the detector after a known regime change
        or at the start of a new trading session.
        """
        self._window.clear()
        self._alerts.clear()
        self._alert_count = 0
