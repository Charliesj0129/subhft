"""Tests for rust_fallback_total Prometheus counter on MarketDataNormalizer."""

from hft_platform.observability.metrics import MetricsRegistry


def test_rust_fallback_total_exists_on_registry():
    registry = MetricsRegistry.get()
    assert registry is not None
    assert hasattr(registry, "rust_fallback_total"), "MetricsRegistry must expose rust_fallback_total counter"


def test_rust_fallback_total_tick_label_increment():
    registry = MetricsRegistry.get()
    counter = registry.rust_fallback_total.labels(type="tick")
    before = counter._value.get()
    counter.inc()
    after = counter._value.get()
    assert after == before + 1.0


def test_rust_fallback_total_bidask_label_increment():
    registry = MetricsRegistry.get()
    counter = registry.rust_fallback_total.labels(type="bidask")
    before = counter._value.get()
    counter.inc()
    after = counter._value.get()
    assert after == before + 1.0


def test_rust_fallback_total_tick_and_bidask_are_independent():
    registry = MetricsRegistry.get()
    tick = registry.rust_fallback_total.labels(type="tick")
    bidask = registry.rust_fallback_total.labels(type="bidask")

    tick_before = tick._value.get()
    bidask_before = bidask._value.get()

    tick.inc()

    assert tick._value.get() == tick_before + 1.0
    assert bidask._value.get() == bidask_before, "Incrementing tick counter must not affect bidask counter"
