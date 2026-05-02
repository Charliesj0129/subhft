"""Unit tests for feed_events_total counter in MetricsRegistry."""

from prometheus_client import CollectorRegistry

from hft_platform.observability.metrics import MetricsRegistry


def _get_counter_value(registry: CollectorRegistry, metric_name: str, labels: dict) -> float:
    """Retrieve a labeled counter value from a prometheus CollectorRegistry."""
    for metric in registry.collect():
        if metric.name == metric_name:
            for sample in metric.samples:
                if all(sample.labels.get(k) == v for k, v in labels.items()):
                    return sample.value
    return 0.0


def test_feed_events_total_tick_increments() -> None:
    """feed_events_total with type='tick' increments correctly."""
    isolated = CollectorRegistry()
    from prometheus_client import Counter

    counter = Counter("feed_events_total_test_tick", "Total feed events test", ["type"], registry=isolated)
    child = counter.labels(type="tick")

    assert _get_counter_value(isolated, "feed_events_total_test_tick", {"type": "tick"}) == 0.0
    child.inc()
    assert _get_counter_value(isolated, "feed_events_total_test_tick", {"type": "tick"}) == 1.0
    child.inc()
    assert _get_counter_value(isolated, "feed_events_total_test_tick", {"type": "tick"}) == 2.0


def test_feed_events_total_bidask_increments() -> None:
    """feed_events_total with type='bidask' increments correctly."""
    isolated = CollectorRegistry()
    from prometheus_client import Counter

    counter = Counter("feed_events_total_test_bidask", "Total feed events test", ["type"], registry=isolated)
    child = counter.labels(type="bidask")

    assert _get_counter_value(isolated, "feed_events_total_test_bidask", {"type": "bidask"}) == 0.0
    child.inc()
    assert _get_counter_value(isolated, "feed_events_total_test_bidask", {"type": "bidask"}) == 1.0


def test_feed_events_total_tick_and_bidask_independent() -> None:
    """tick and bidask label children are independent counters."""
    isolated = CollectorRegistry()
    from prometheus_client import Counter

    counter = Counter("feed_events_total_test_indep", "Total feed events test", ["type"], registry=isolated)
    tick_child = counter.labels(type="tick")
    bidask_child = counter.labels(type="bidask")

    tick_child.inc()
    tick_child.inc()
    tick_child.inc()
    bidask_child.inc()

    assert _get_counter_value(isolated, "feed_events_total_test_indep", {"type": "tick"}) == 3.0
    assert _get_counter_value(isolated, "feed_events_total_test_indep", {"type": "bidask"}) == 1.0


def test_metrics_registry_feed_events_total_defined() -> None:
    """MetricsRegistry exposes feed_events_total and it supports expected label values."""
    registry = MetricsRegistry.get()
    assert hasattr(registry, "feed_events_total"), "MetricsRegistry must define feed_events_total"

    # Verify labeling works for both supported type values without error
    tick_child = registry.feed_events_total.labels(type="tick")
    bidask_child = registry.feed_events_total.labels(type="bidask")

    # Calling inc() should not raise
    tick_child.inc(0)
    bidask_child.inc(0)
