"""Unit tests for alpha.drift_detector — Signal Drift Detection (Unit 5)."""

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


def _feed(detector: DriftDetector, values: list[float]) -> list[DriftAlert]:
    """Feed a sequence of values and collect non-None alerts."""
    alerts = []
    for v in values:
        alert = detector.observe(v)
        if alert is not None:
            alerts.append(alert)
    return alerts


def _make_values(mean: float, count: int) -> list[float]:
    """Return a deterministic list of ``count`` values all equal to ``mean``."""
    return [mean] * count


# ---------------------------------------------------------------------------
# DriftDetectorConfig
# ---------------------------------------------------------------------------


class TestDriftDetectorConfig:
    def test_defaults(self) -> None:
        cfg = DriftDetectorConfig()
        assert cfg.window_size == 100
        assert cfg.z_threshold == 3.0
        assert cfg.max_alerts == 10

    def test_custom_values(self) -> None:
        cfg = DriftDetectorConfig(window_size=50, z_threshold=2.0, max_alerts=5)
        assert cfg.window_size == 50
        assert cfg.z_threshold == 2.0
        assert cfg.max_alerts == 5

    def test_frozen(self) -> None:
        cfg = DriftDetectorConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.window_size = 200  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DriftAlert
# ---------------------------------------------------------------------------


class TestDriftAlert:
    def test_fields(self) -> None:
        alert = DriftAlert(
            z_score=4.5,
            rolling_mean=1.5,
            baseline_mean=0.0,
            timestamp_ns=1_000_000,
        )
        assert alert.z_score == 4.5
        assert alert.rolling_mean == 1.5
        assert alert.baseline_mean == 0.0
        assert alert.timestamp_ns == 1_000_000

    def test_frozen(self) -> None:
        alert = DriftAlert(z_score=1.0, rolling_mean=0.0, baseline_mean=0.0, timestamp_ns=0)
        with pytest.raises((AttributeError, TypeError)):
            alert.z_score = 99.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# No drift — same distribution
# ---------------------------------------------------------------------------


class TestNoDrift:
    def test_same_distribution_no_alerts(self) -> None:
        """Values drawn from baseline distribution must not trigger alerts."""
        baseline_mean = 0.0
        baseline_std = 1.0
        cfg = DriftDetectorConfig(window_size=10, z_threshold=3.0, max_alerts=5)
        detector = DriftDetector(baseline_mean, baseline_std, cfg)

        # Feed 50 values equal to the baseline mean — z_score will be 0.
        values = _make_values(baseline_mean, 50)
        alerts = _feed(detector, values)

        assert alerts == []
        assert detector.alert_count == 0

    def test_small_perturbation_below_threshold_no_alerts(self) -> None:
        """Values slightly above mean but z-score < threshold → no alert."""
        cfg = DriftDetectorConfig(window_size=10, z_threshold=3.0, max_alerts=5)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0, config=cfg)

        # rolling_mean will be 0.1 → z = 0.1 / 1.0 = 0.1 < 3.0
        values = _make_values(0.1, 50)
        alerts = _feed(detector, values)

        assert alerts == []
        assert detector.alert_count == 0


# ---------------------------------------------------------------------------
# Window not full — no alerts yet
# ---------------------------------------------------------------------------


class TestWindowNotFull:
    def test_no_alert_before_window_full(self) -> None:
        """Detector must not emit alerts before the rolling window is filled."""
        cfg = DriftDetectorConfig(window_size=20, z_threshold=1.0, max_alerts=5)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0, config=cfg)

        # Feed 19 heavily drifted values (window_size - 1 = not full yet).
        for i in range(19):
            result = detector.observe(value=100.0, timestamp_ns=i)
            assert result is None, f"Got unexpected alert at index {i}"

        assert detector.alert_count == 0

    def test_alert_fires_exactly_at_window_full(self) -> None:
        """The 20th observation (filling the window) should trigger an alert."""
        cfg = DriftDetectorConfig(window_size=20, z_threshold=1.0, max_alerts=5)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0, config=cfg)

        for _ in range(19):
            detector.observe(value=100.0)

        # 20th observation fills the window → drift must be detected.
        alert = detector.observe(value=100.0)
        assert alert is not None
        assert alert.alert_count if hasattr(alert, "alert_count") else True
        assert detector.alert_count == 1


# ---------------------------------------------------------------------------
# Mean shift → alerts triggered
# ---------------------------------------------------------------------------


class TestMeanShift:
    def test_large_mean_shift_triggers_alert(self) -> None:
        """A large mean shift should trigger at least one alert."""
        baseline_mean = 0.0
        baseline_std = 1.0
        cfg = DriftDetectorConfig(window_size=10, z_threshold=3.0, max_alerts=5)
        detector = DriftDetector(baseline_mean, baseline_std, cfg)

        # rolling_mean = 10.0 → z_score = |10.0 - 0.0| / 1.0 = 10.0 > 3.0
        values = _make_values(10.0, 20)
        alerts = _feed(detector, values)

        assert len(alerts) > 0
        assert detector.alert_count > 0

    def test_alert_fields_correct(self) -> None:
        """Alert fields must accurately reflect the drift state."""
        baseline_mean = 0.0
        baseline_std = 1.0
        cfg = DriftDetectorConfig(window_size=10, z_threshold=3.0, max_alerts=5)
        detector = DriftDetector(baseline_mean, baseline_std, cfg)

        values = _make_values(10.0, 10)
        alerts = _feed(detector, values)

        assert len(alerts) >= 1
        first_alert = alerts[0]
        assert first_alert.baseline_mean == baseline_mean
        assert first_alert.rolling_mean == pytest.approx(10.0, abs=1e-9)
        expected_z = abs(10.0 - 0.0) / 1.0
        assert first_alert.z_score == pytest.approx(expected_z, rel=1e-6)

    def test_alert_z_score_uses_abs(self) -> None:
        """Negative drift (mean below baseline) must also trigger alerts."""
        cfg = DriftDetectorConfig(window_size=10, z_threshold=3.0, max_alerts=5)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0, config=cfg)

        # Shift to -10 — absolute z = 10 > 3.0
        values = _make_values(-10.0, 20)
        alerts = _feed(detector, values)

        assert len(alerts) > 0
        assert all(a.z_score > 0 for a in alerts)

    def test_timestamp_stored_in_alert(self) -> None:
        """timestamp_ns passed to observe() must be forwarded into the alert."""
        cfg = DriftDetectorConfig(window_size=5, z_threshold=1.0, max_alerts=5)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0, config=cfg)

        for i in range(4):
            detector.observe(value=100.0, timestamp_ns=i)

        expected_ts = 999_999_999
        alert = detector.observe(value=100.0, timestamp_ns=expected_ts)

        assert alert is not None
        assert alert.timestamp_ns == expected_ts


# ---------------------------------------------------------------------------
# max_alerts cap
# ---------------------------------------------------------------------------


class TestMaxAlertsCap:
    def test_max_alerts_respected(self) -> None:
        """No more than max_alerts alerts must be emitted."""
        max_alerts = 3
        cfg = DriftDetectorConfig(window_size=5, z_threshold=1.0, max_alerts=max_alerts)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0, config=cfg)

        # Feed 100 heavily drifted values — only max_alerts should be returned.
        values = _make_values(50.0, 100)
        alerts = _feed(detector, values)

        assert len(alerts) == max_alerts
        assert detector.alert_count == max_alerts

    def test_no_alert_after_cap(self) -> None:
        """After reaching max_alerts, observe() must return None."""
        cfg = DriftDetectorConfig(window_size=5, z_threshold=1.0, max_alerts=2)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0, config=cfg)

        # Fill up all alert slots.
        _feed(detector, _make_values(50.0, 50))
        assert detector.alert_count == 2

        # Further observations must not produce alerts.
        for _ in range(20):
            result = detector.observe(value=50.0)
            assert result is None

        assert detector.alert_count == 2


# ---------------------------------------------------------------------------
# Zero baseline_std — graceful no-op
# ---------------------------------------------------------------------------


class TestZeroBaselineStd:
    def test_zero_std_no_alerts(self) -> None:
        """A zero baseline_std must not cause division by zero or alerts."""
        cfg = DriftDetectorConfig(window_size=5, z_threshold=1.0, max_alerts=5)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=0.0, config=cfg)

        values = _make_values(100.0, 20)
        alerts = _feed(detector, values)

        assert alerts == []
        assert detector.alert_count == 0

    def test_negative_std_no_alerts(self) -> None:
        """A negative baseline_std (invalid) must be treated as non-positive — no alerts."""
        cfg = DriftDetectorConfig(window_size=5, z_threshold=1.0, max_alerts=5)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=-1.0, config=cfg)

        values = _make_values(100.0, 20)
        alerts = _feed(detector, values)

        assert alerts == []
        assert detector.alert_count == 0


# ---------------------------------------------------------------------------
# reset()
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_window(self) -> None:
        """After reset, the window should be empty — no alerts on next window-1 values."""
        cfg = DriftDetectorConfig(window_size=5, z_threshold=1.0, max_alerts=5)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0, config=cfg)

        # Trigger some alerts to confirm state is being set.
        _feed(detector, _make_values(50.0, 10))
        assert detector.alert_count > 0

        detector.reset()
        assert detector.alert_count == 0

        # Feed 4 values (window_size - 1) — no alert should fire.
        for _ in range(4):
            result = detector.observe(value=50.0)
            assert result is None

    def test_reset_allows_new_alerts(self) -> None:
        """After reset, the detector must be able to emit fresh alerts."""
        cfg = DriftDetectorConfig(window_size=5, z_threshold=1.0, max_alerts=2)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0, config=cfg)

        # Exhaust alert cap.
        _feed(detector, _make_values(50.0, 50))
        assert detector.alert_count == 2

        # Reset and verify detector is operational again.
        detector.reset()
        assert detector.alert_count == 0

        new_alerts = _feed(detector, _make_values(50.0, 10))
        assert len(new_alerts) > 0
        assert detector.alert_count > 0

    def test_reset_idempotent(self) -> None:
        """Calling reset() on a fresh detector must not raise."""
        cfg = DriftDetectorConfig(window_size=10, z_threshold=3.0, max_alerts=5)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0, config=cfg)
        detector.reset()
        detector.reset()
        assert detector.alert_count == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_single_value_window(self) -> None:
        """window_size=1 should alert immediately on first drifted value."""
        cfg = DriftDetectorConfig(window_size=1, z_threshold=1.0, max_alerts=5)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0, config=cfg)

        alert = detector.observe(value=10.0)
        assert alert is not None
        assert alert.z_score == pytest.approx(10.0, rel=1e-6)

    def test_exactly_at_threshold_no_alert(self) -> None:
        """z_score == z_threshold must NOT trigger an alert (strict inequality)."""
        cfg = DriftDetectorConfig(window_size=5, z_threshold=3.0, max_alerts=5)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0, config=cfg)

        # rolling_mean = 3.0 → z_score = 3.0 == threshold (not strictly greater)
        values = _make_values(3.0, 10)
        alerts = _feed(detector, values)

        assert alerts == []
        assert detector.alert_count == 0

    def test_just_above_threshold_triggers_alert(self) -> None:
        """z_score just above z_threshold must trigger an alert."""
        cfg = DriftDetectorConfig(window_size=5, z_threshold=3.0, max_alerts=5)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0, config=cfg)

        # rolling_mean = 3.0001 → z_score > 3.0
        values = _make_values(3.0001, 10)
        alerts = _feed(detector, values)

        assert len(alerts) > 0

    def test_default_timestamp_zero(self) -> None:
        """When timestamp_ns is omitted, alert.timestamp_ns must be 0."""
        cfg = DriftDetectorConfig(window_size=5, z_threshold=1.0, max_alerts=5)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0, config=cfg)

        for _ in range(4):
            detector.observe(value=50.0)
        alert = detector.observe(value=50.0)  # no timestamp_ns

        assert alert is not None
        assert alert.timestamp_ns == 0

    def test_alert_count_property(self) -> None:
        """alert_count property must equal the number of returned alerts."""
        cfg = DriftDetectorConfig(window_size=5, z_threshold=1.0, max_alerts=10)
        detector = DriftDetector(baseline_mean=0.0, baseline_std=1.0, config=cfg)

        alerts = _feed(detector, _make_values(50.0, 30))
        assert detector.alert_count == len(alerts)
