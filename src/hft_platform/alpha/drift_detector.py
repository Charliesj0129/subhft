"""Drift detector — online statistical drift detection for alpha signals.

Maintains a sliding window of observed values and detects distribution
shifts using z-score based alerting.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import structlog

logger = structlog.get_logger("alpha.drift_detector")


@dataclass(frozen=True, slots=True)
class DriftDetectorConfig:
    alpha_id: str
    window_size: int = 100
    z_score_threshold: float = 3.0
    max_alerts: int = 1000
    min_window_fill: int = 20


@dataclass(frozen=True, slots=True)
class DriftAlert:
    alpha_id: str
    observation: float
    mean: float
    std: float
    z_score: float
    alert_number: int


class DriftDetector:
    """Online drift detector using z-score over a rolling window.

    Attributes
    ----------
    config : DriftDetectorConfig
        Configuration including window size, z-score threshold, etc.
    """

    __slots__ = (
        "_config",
        "_window",
        "_alerts",
        "_total_observations",
    )

    def __init__(self, config: DriftDetectorConfig) -> None:
        self._config = config
        self._window: deque[float] = deque(maxlen=config.window_size)
        self._alerts: list[DriftAlert] = []
        self._total_observations: int = 0

    @property
    def config(self) -> DriftDetectorConfig:
        return self._config

    @property
    def alert_count(self) -> int:
        """Total number of drift alerts emitted."""
        return len(self._alerts)

    @property
    def alerts(self) -> list[DriftAlert]:
        """List of all drift alerts (read-only view)."""
        return list(self._alerts)

    @property
    def total_observations(self) -> int:
        """Total number of observations processed."""
        return self._total_observations

    def observe(self, value: float) -> DriftAlert | None:
        """Process a new observation.

        Returns a DriftAlert if a drift is detected, else None.
        Window must have at least min_window_fill observations before alerting.
        """
        self._total_observations += 1
        self._window.append(value)

        if len(self._window) < self._config.min_window_fill:
            return None

        n = len(self._window)
        mean = sum(self._window) / n
        variance = sum((x - mean) ** 2 for x in self._window) / n
        std = math.sqrt(variance)

        if std < 1e-12:
            # Zero std — cannot compute z-score; no drift
            return None

        z_score = abs((value - mean) / std)
        if z_score < self._config.z_score_threshold:
            return None

        # Drift detected
        if len(self._alerts) >= self._config.max_alerts:
            logger.warning(
                "drift_detector.max_alerts_cap_reached",
                alpha_id=self._config.alpha_id,
                max_alerts=self._config.max_alerts,
            )
            return None

        alert = DriftAlert(
            alpha_id=self._config.alpha_id,
            observation=value,
            mean=mean,
            std=std,
            z_score=z_score,
            alert_number=len(self._alerts) + 1,
        )
        self._alerts.append(alert)
        logger.info(
            "drift_detector.alert",
            alpha_id=self._config.alpha_id,
            z_score=z_score,
            threshold=self._config.z_score_threshold,
            alert_number=alert.alert_number,
        )
        return alert

    def reset(self) -> None:
        """Reset the detector state (window and alerts)."""
        self._window.clear()
        self._alerts.clear()
        self._total_observations = 0
        logger.info("drift_detector.reset", alpha_id=self._config.alpha_id)
