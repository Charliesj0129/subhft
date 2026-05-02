"""Tests for fills_total Prometheus counter on MetricsRegistry."""

from hft_platform.observability.metrics import MetricsRegistry


def test_fills_total_counter_exists() -> None:
    """MetricsRegistry should expose a fills_total counter attribute."""
    registry = MetricsRegistry()
    assert hasattr(registry, "fills_total"), "fills_total counter not found on MetricsRegistry"


def test_fills_total_counter_increments() -> None:
    """fills_total counter should increment without error."""
    registry = MetricsRegistry()
    before = registry.fills_total._value.get()
    registry.fills_total.inc()
    after = registry.fills_total._value.get()
    assert after == before + 1, f"Expected {before + 1}, got {after}"


def test_fills_total_counter_starts_at_zero() -> None:
    """fills_total counter should start at zero on fresh instantiation."""
    registry = MetricsRegistry()
    assert registry.fills_total._value.get() == 0, "fills_total should start at 0"
