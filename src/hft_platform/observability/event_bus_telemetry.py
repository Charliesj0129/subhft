"""Prometheus adapter for the event bus telemetry port."""

from typing import cast

from hft_platform.engine.event_bus_telemetry import (
    ConsumerLagSink,
    EventCounterSink,
)
from hft_platform.observability.metrics import MetricsRegistry


class PrometheusEventBusTelemetry:
    """Map event-bus telemetry operations to the platform metrics registry."""

    __slots__ = ("_consumer_lag", "gap_event_counter", "overflow_counter")

    def __init__(self, registry: MetricsRegistry) -> None:
        self._consumer_lag = registry.bus_consumer_lag
        self.overflow_counter = cast(EventCounterSink, registry.bus_overflow_total)
        self.gap_event_counter = cast(EventCounterSink, registry.bus_gap_events_total)

    def bind_consumer(self, consumer: str) -> ConsumerLagSink:
        return cast(
            ConsumerLagSink,
            self._consumer_lag.labels(consumer=consumer),
        )
