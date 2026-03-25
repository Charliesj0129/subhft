"""Tests for backup-related Prometheus metrics."""

from __future__ import annotations


def test_metrics_registry_has_backup_gauges():
    from hft_platform.observability.metrics import MetricsRegistry

    m = MetricsRegistry.get()
    assert hasattr(m, "backup_last_success_ts")
    assert hasattr(m, "backup_size_bytes")
    assert hasattr(m, "backup_duration_seconds")
    assert hasattr(m, "backup_retained_count")


def test_backup_gauges_are_settable():
    from hft_platform.observability.metrics import MetricsRegistry

    m = MetricsRegistry.get()
    m.backup_last_success_ts.set(1711324800.0)
    m.backup_size_bytes.set(1024)
    m.backup_duration_seconds.set(5.5)
    m.backup_retained_count.set(15)
    # If no exception, gauges work correctly
