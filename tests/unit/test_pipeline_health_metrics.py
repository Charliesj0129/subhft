"""Tests for pipeline health metrics registration (EC-5 fix).

Verifies that MetricsRegistry correctly exposes pipeline_health_state
and pipeline_degradation_events_total, and that PipelineHealthTracker
actually updates the gauge on state transitions instead of silently
swallowing the AttributeError.
"""

import pytest
from prometheus_client import Counter, Gauge


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Ensure a fresh MetricsRegistry for each test."""
    from hft_platform.observability.metrics import MetricsRegistry

    MetricsRegistry._instance = None
    yield
    MetricsRegistry._instance = None


def _make_registry():
    from hft_platform.observability.metrics import MetricsRegistry

    MetricsRegistry._instance = None
    return MetricsRegistry()


class TestPipelineHealthMetricsRegistered:
    def test_pipeline_health_state_is_gauge(self):
        registry = _make_registry()
        assert hasattr(registry, "pipeline_health_state")
        assert isinstance(registry.pipeline_health_state, Gauge)

    def test_pipeline_degradation_events_total_is_counter(self):
        registry = _make_registry()
        assert hasattr(registry, "pipeline_degradation_events_total")
        assert isinstance(registry.pipeline_degradation_events_total, Counter)

    def test_pipeline_health_state_initial_value_is_zero(self):
        registry = _make_registry()
        # Gauge should start at 0 (HEALTHY)
        value = registry.pipeline_health_state._value.get()
        assert value == 0.0

    def test_pipeline_degradation_events_total_initial_value_is_zero(self):
        registry = _make_registry()
        value = registry.pipeline_degradation_events_total._value.get()
        assert value == 0.0


class TestPipelineHealthTrackerUpdatesMetrics:
    """Verify PipelineHealthTracker.record_event propagates to Prometheus metrics."""

    def test_data_loss_event_sets_gauge_to_three(self):
        """DATA_LOSS state == 3; gauge must reflect this after transition."""
        from hft_platform.observability.metrics import MetricsRegistry
        from hft_platform.recorder.health import PipelineHealthTracker

        # Force fresh singleton so tracker picks up new registry
        MetricsRegistry._instance = None
        registry = MetricsRegistry()
        MetricsRegistry._instance = registry

        tracker = PipelineHealthTracker()
        # Inject the registry directly to bypass any import caching
        tracker._metrics = registry

        # Trigger a transition from HEALTHY -> DATA_LOSS
        tracker.record_event("data_loss")

        gauge_value = registry.pipeline_health_state._value.get()
        assert gauge_value == 3.0, f"Expected gauge=3 (DATA_LOSS) after data_loss event, got {gauge_value}"

    def test_state_transition_increments_counter(self):
        """Each state transition must increment pipeline_degradation_events_total."""
        from hft_platform.observability.metrics import MetricsRegistry
        from hft_platform.recorder.health import PipelineHealthTracker

        MetricsRegistry._instance = None
        registry = MetricsRegistry()
        MetricsRegistry._instance = registry

        tracker = PipelineHealthTracker()
        tracker._metrics = registry

        before = registry.pipeline_degradation_events_total._value.get()

        # HEALTHY -> DEGRADED
        tracker.record_event("wal_fallback")
        after = registry.pipeline_degradation_events_total._value.get()

        assert after == before + 1, (
            f"Expected counter to increment by 1 on state transition, got before={before} after={after}"
        )

    def test_no_transition_does_not_increment_counter(self):
        """Repeated same-state events must NOT increment the transition counter."""
        from hft_platform.observability.metrics import MetricsRegistry
        from hft_platform.recorder.health import PipelineHealthTracker

        MetricsRegistry._instance = None
        registry = MetricsRegistry()
        MetricsRegistry._instance = registry

        tracker = PipelineHealthTracker()
        tracker._metrics = registry

        # First event transitions HEALTHY -> DEGRADED
        tracker.record_event("wal_fallback")
        after_first = registry.pipeline_degradation_events_total._value.get()

        # Second identical event stays DEGRADED (no new transition)
        tracker.record_event("wal_fallback")
        after_second = registry.pipeline_degradation_events_total._value.get()

        assert after_first == after_second, "Counter must not increment when state does not change"
