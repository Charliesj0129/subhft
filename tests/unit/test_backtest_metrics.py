"""WU-12: Tests for backtest Prometheus metrics."""

from __future__ import annotations

from hft_platform.backtest import _metrics


def test_metrics_disabled_by_default():
    """Metrics are disabled when HFT_BACKTEST_METRICS is not set."""
    # Default is off — these should be no-ops
    assert not _metrics.is_enabled()


def test_record_tick_noop_when_disabled():
    """record_tick is a no-op when disabled."""
    _metrics.record_tick("TEST", "feed")  # should not raise


def test_record_tick_overhead_noop_when_disabled():
    """record_tick_overhead is a no-op when disabled."""
    _metrics.record_tick_overhead("feed", 42.0)  # should not raise


def test_record_fill_noop_when_disabled():
    """record_fill is a no-op when disabled."""
    _metrics.record_fill("TEST", "buy")  # should not raise


def test_record_run_duration_noop_when_disabled():
    """record_run_duration is a no-op when disabled."""
    _metrics.record_run_duration("feed", 1.5)  # should not raise


def test_ensure_metrics_idempotent():
    """_ensure_metrics can be called multiple times safely."""
    _metrics._ensure_metrics()
    _metrics._ensure_metrics()
    assert _metrics._metrics_initialized
