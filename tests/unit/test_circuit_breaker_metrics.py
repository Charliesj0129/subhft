"""Tests for Prometheus metrics in StrategyCircuitBreakerManager."""

from __future__ import annotations

from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.order.circuit_breaker import StrategyCircuitBreakerManager


def _gauge_value(registry, component: str) -> float:
    """Read current value of circuit_breaker_state gauge for given component."""
    return registry.circuit_breaker_state.labels(component=component)._value.get()


def test_record_failure_trip_sets_gauge() -> None:
    """When enough failures trip the breaker, gauge should be set to 1."""
    registry = MetricsRegistry.get()
    manager = StrategyCircuitBreakerManager(default_threshold=2, default_timeout_s=60)
    strategy_id = "test_trip_strategy"
    component = f"strategy:{strategy_id}"

    # First failure — should not trip (threshold=2)
    tripped = manager.record_failure(strategy_id)
    assert tripped is False

    # Second failure — should trip
    tripped = manager.record_failure(strategy_id)
    assert tripped is True

    # Gauge must be 1 (open/tripped)
    assert _gauge_value(registry, component) == 1.0


def test_record_success_resets_gauge() -> None:
    """After a trip, recording success should reset gauge to 0."""
    registry = MetricsRegistry.get()
    manager = StrategyCircuitBreakerManager(default_threshold=1, default_timeout_s=60)
    strategy_id = "test_reset_strategy"
    component = f"strategy:{strategy_id}"

    # Trip the breaker
    tripped = manager.record_failure(strategy_id)
    assert tripped is True
    assert _gauge_value(registry, component) == 1.0

    # Record success — gauge should reset to 0
    manager.record_success(strategy_id)
    assert _gauge_value(registry, component) == 0.0
