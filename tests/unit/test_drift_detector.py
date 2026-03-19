"""Tests for DriftDetector."""

from __future__ import annotations

import pytest

from hft_platform.alpha.drift_detector import (
    DriftAlert,
    DriftDetector,
    DriftDetectorConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides: object) -> DriftDetectorConfig:
    defaults = {
        "alpha_id": "test_alpha",
        "window_size": 50,
        "z_score_threshold": 3.0,
        "max_alerts": 100,
        "min_window_fill": 10,
    }
    defaults.update(overrides)
    return DriftDetectorConfig(**defaults)  # type: ignore[arg-type]


def _stable_observations(n: int = 50, mean: float = 0.0, std: float = 1.0) -> list[float]:
    """Return n observations drawn from a deterministic near-Gaussian sequence."""
    import random

    rng = random.Random(42)
    return [rng.gauss(mean, std) for _ in range(n)]


# ---------------------------------------------------------------------------
# No-drift tests
# ---------------------------------------------------------------------------


class TestNoDrift:
    def test_stable_signal_no_alerts(self) -> None:
        cfg = _cfg(z_score_threshold=5.0)
        detector = DriftDetector(cfg)
        for v in _stable_observations(100, mean=0.0, std=0.1):
            detector.observe(v)
        assert detector.alert_count == 0

    def test_window_not_full_no_alerts(self) -> None:
        cfg = _cfg(min_window_fill=20)
        detector = DriftDetector(cfg)
        # Feed fewer than min_window_fill observations
        for i in range(15):
            result = detector.observe(float(i * 100))  # large values but window not full
        assert detector.alert_count == 0

    def test_zero_std_no_alert(self) -> None:
        """Constant signal has zero std — cannot compute z-score, no alert."""
        cfg = _cfg(min_window_fill=5)
        detector = DriftDetector(cfg)
        for _ in range(20):
            detector.observe(1.0)
        assert detector.alert_count == 0


# ---------------------------------------------------------------------------
# Drift detection tests
# ---------------------------------------------------------------------------


class TestDriftDetection:
    def test_mean_shift_triggers_alert(self) -> None:
        """After stable observations, a sudden extreme outlier should trigger an alert."""
        cfg = _cfg(z_score_threshold=3.0, min_window_fill=20, window_size=50)
        detector = DriftDetector(cfg)
        # Fill window with stable values
        for _ in range(40):
            detector.observe(0.0)
        # Inject a strong outlier
        alert = detector.observe(1000.0)
        assert alert is not None
        assert isinstance(alert, DriftAlert)
        assert alert.z_score > 3.0
        assert detector.alert_count == 1

    def test_alert_contains_correct_fields(self) -> None:
        cfg = _cfg(z_score_threshold=2.0, min_window_fill=10, window_size=20)
        detector = DriftDetector(cfg)
        for _ in range(15):
            detector.observe(0.0)
        alert = detector.observe(100.0)
        assert alert is not None
        assert alert.alpha_id == cfg.alpha_id
        assert alert.observation == pytest.approx(100.0)
        # mean includes the current value (computed after appending)
        assert alert.mean > 0.0  # non-zero since 100.0 is included
        assert alert.std > 0
        assert alert.z_score >= cfg.z_score_threshold
        assert alert.alert_number == 1

    def test_alert_number_increments(self) -> None:
        cfg = _cfg(z_score_threshold=2.0, min_window_fill=5, window_size=10)
        detector = DriftDetector(cfg)
        for _ in range(10):
            detector.observe(0.0)
        # Inject multiple outliers
        detector.observe(500.0)
        detector.observe(500.0)
        assert detector.alert_count == 2
        assert detector.alerts[0].alert_number == 1
        assert detector.alerts[1].alert_number == 2

    def test_no_alert_below_threshold(self) -> None:
        cfg = _cfg(z_score_threshold=10.0, min_window_fill=10)
        detector = DriftDetector(cfg)
        for _ in range(20):
            detector.observe(0.0)
        # Small deviation — should not trigger
        result = detector.observe(0.5)
        assert result is None
        assert detector.alert_count == 0


# ---------------------------------------------------------------------------
# max_alerts cap
# ---------------------------------------------------------------------------


class TestMaxAlertsCap:
    def test_max_alerts_cap(self) -> None:
        """Once max_alerts is reached, no further alerts are stored."""
        cfg = _cfg(z_score_threshold=2.0, min_window_fill=5, window_size=20, max_alerts=3)
        detector = DriftDetector(cfg)
        # Fill window with stable zero values
        for _ in range(20):
            detector.observe(0.0)
        # Inject outliers interleaved with stable values to keep z-score high
        for i in range(15):
            # Stable values reset the window context
            for _ in range(10):
                detector.observe(0.0)
            detector.observe(1000.0)
        assert detector.alert_count == 3


# ---------------------------------------------------------------------------
# reset tests
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_window_and_alerts(self) -> None:
        cfg = _cfg(z_score_threshold=2.0, min_window_fill=5, window_size=10)
        detector = DriftDetector(cfg)
        for _ in range(10):
            detector.observe(0.0)
        detector.observe(500.0)
        assert detector.alert_count == 1
        detector.reset()
        assert detector.alert_count == 0
        assert detector.total_observations == 0
        # After reset, window is empty so injecting outlier won't alert
        result = detector.observe(500.0)
        assert result is None  # window not full

    def test_total_observations_resets(self) -> None:
        cfg = _cfg()
        detector = DriftDetector(cfg)
        for i in range(20):
            detector.observe(float(i))
        assert detector.total_observations == 20
        detector.reset()
        assert detector.total_observations == 0
