"""Telemetry port for the event bus hot path."""

from typing import Protocol


class ConsumerLagSink(Protocol):
    """Pre-bound consumer lag metric."""

    def set(self, lag: int) -> None:
        """Record the current distance behind the writer cursor."""


class EventCounterSink(Protocol):
    """Pre-bound event counter."""

    def inc(self) -> None:
        """Increment the counter by one."""


class EventBusTelemetry(Protocol):
    """Observability operations used by :class:`RingBufferBus`."""

    overflow_counter: EventCounterSink
    gap_event_counter: EventCounterSink

    def bind_consumer(self, consumer: str) -> ConsumerLagSink:
        """Return a lag sink bound to one consumer label."""


class _NoopConsumerLagSink:
    __slots__ = ()

    def set(self, lag: int) -> None:
        return None


class _NoopEventCounterSink:
    __slots__ = ()

    def inc(self) -> None:
        return None


class _NoopEventBusTelemetry:
    __slots__ = ("gap_event_counter", "overflow_counter")

    def __init__(self) -> None:
        self.overflow_counter = NOOP_EVENT_COUNTER_SINK
        self.gap_event_counter = NOOP_EVENT_COUNTER_SINK

    def bind_consumer(self, consumer: str) -> ConsumerLagSink:
        return NOOP_CONSUMER_LAG_SINK


NOOP_CONSUMER_LAG_SINK: ConsumerLagSink = _NoopConsumerLagSink()
NOOP_EVENT_COUNTER_SINK: EventCounterSink = _NoopEventCounterSink()
NOOP_EVENT_BUS_TELEMETRY: EventBusTelemetry = _NoopEventBusTelemetry()
