import pytest

from hft_platform.observability.metrics import MetricsRegistry


@pytest.fixture
def metrics():
    return MetricsRegistry.get()


def test_feed_gap_metric(metrics):
    # Simulate feed silence
    # In real world, Prometheus alert rules catch rate() == 0
    # Here we verify we *can* increment it, so absence implies gap
    initial = metrics.feed_events_total.labels(type="tick")._value.get()
    metrics.feed_events_total.labels(type="tick").inc()
    assert metrics.feed_events_total.labels(type="tick")._value.get() == initial + 1


def test_reject_rate_metric(metrics):
    initial_actions = metrics.order_actions_total.labels(type="new")._value.get()
    initial_rejects = metrics.order_reject_total._value.get()

    metrics.order_actions_total.labels(type="new").inc(10)
    metrics.order_reject_total.inc(5)

    actions = metrics.order_actions_total.labels(type="new")._value.get() - initial_actions
    rejects = metrics.order_reject_total._value.get() - initial_rejects

    rate = rejects / actions
    # Alert rule is > 0.05
    assert rate == 0.5
    assert rate > 0.05


def test_stormguard_halt_metric(metrics):
    # Simulate HALT
    metrics.stormguard_mode.labels(strategy="strat_a").set(3)
    val = metrics.stormguard_mode.labels(strategy="strat_a")._value.get()
    assert val == 3
