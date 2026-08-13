"""Tests for the event bus telemetry dependency boundary."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.engine.event_bus_telemetry import ConsumerLagSink
from hft_platform.events import GapEvent


class _RecordingLagSink:
    __slots__ = ("values",)

    def __init__(self) -> None:
        self.values: list[int] = []

    def set(self, lag: int) -> None:
        self.values.append(lag)


class _RecordingCounter:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0

    def inc(self) -> None:
        self.value += 1


class _RecordingTelemetry:
    __slots__ = (
        "bound_consumers",
        "gap_event_counter",
        "lag_sink",
        "overflow_counter",
    )

    def __init__(self) -> None:
        self.bound_consumers: list[str] = []
        self.gap_event_counter = _RecordingCounter()
        self.lag_sink = _RecordingLagSink()
        self.overflow_counter = _RecordingCounter()

    def bind_consumer(self, consumer: str) -> ConsumerLagSink:
        self.bound_consumers.append(consumer)
        return self.lag_sink


def test_default_telemetry_is_safe_for_direct_bus_construction() -> None:
    bus = RingBufferBus(size=4)
    bus.publish_nowait("event")

    async def _consume_one() -> object:
        return await bus.consume(start_cursor=-1).__anext__()

    assert asyncio.run(_consume_one()) == "event"


def test_consume_reports_overflow_gap_and_lag_through_injected_port() -> None:
    telemetry = _RecordingTelemetry()
    bus = RingBufferBus(size=2, telemetry=telemetry)
    bus.publish_many_nowait(["e1", "e2", "e3", "e4", "e5"])

    async def _consume_one() -> object:
        return await bus.consume(start_cursor=-1, consumer_name="telemetry-probe").__anext__()

    event = asyncio.run(_consume_one())

    assert isinstance(event, GapEvent)
    assert telemetry.bound_consumers == ["telemetry-probe"]
    assert telemetry.overflow_counter.value == 1
    assert telemetry.gap_event_counter.value == 1
    assert telemetry.lag_sink.values == [2]


def test_consume_batch_reports_through_the_same_injected_port() -> None:
    telemetry = _RecordingTelemetry()
    bus = RingBufferBus(size=2, telemetry=telemetry)
    bus.publish_many_nowait(["e1", "e2", "e3", "e4", "e5"])

    async def _consume_one_batch() -> list[object]:
        return await bus.consume_batch(
            batch_size=2,
            start_cursor=-1,
            consumer_name="batch-telemetry-probe",
        ).__anext__()

    batch = asyncio.run(_consume_one_batch())

    assert len(batch) == 1
    assert isinstance(batch[0], GapEvent)
    assert telemetry.bound_consumers == ["batch-telemetry-probe"]
    assert telemetry.overflow_counter.value == 1
    assert telemetry.gap_event_counter.value == 1
    assert telemetry.lag_sink.values == [2]


def test_event_bus_has_no_observability_import() -> None:
    source_path = Path(__file__).resolve().parents[2] / "src" / "hft_platform" / "engine" / "event_bus.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden_imports: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            forbidden_imports.extend(
                alias.name for alias in node.names if alias.name.startswith("hft_platform.observability")
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("hft_platform.observability"):
                forbidden_imports.append(module)

    assert forbidden_imports == []
