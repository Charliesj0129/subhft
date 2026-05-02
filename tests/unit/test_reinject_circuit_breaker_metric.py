"""Test that recorder_reinject_circuit_breaker_drops_total counter is registered
through MetricsRegistry and appears in the Prometheus registry after init."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY

from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.recorder.batcher import Batcher


def _make_registry() -> MetricsRegistry:
    MetricsRegistry._instance = None
    return MetricsRegistry.get()


def test_reinject_counter_present_on_metrics_registry():
    """MetricsRegistry must expose recorder_reinject_circuit_breaker_drops_total."""
    reg = _make_registry()
    assert hasattr(reg, "recorder_reinject_circuit_breaker_drops_total"), (
        "MetricsRegistry is missing recorder_reinject_circuit_breaker_drops_total"
    )


def test_reinject_counter_appears_in_prometheus_registry():
    """After MetricsRegistry init the counter must be visible in REGISTRY."""
    _make_registry()
    names = set(REGISTRY._names_to_collectors.keys())  # type: ignore[attr-defined]
    assert "recorder_reinject_circuit_breaker_drops_total" in names, (
        "recorder_reinject_circuit_breaker_drops_total not found in Prometheus REGISTRY; "
        "it is likely still registered at module level and gets swept by _unregister_all_custom_metrics"
    )


def test_reinject_counter_survives_second_metrics_registry_init():
    """Creating a second MetricsRegistry instance must not orphan the counter."""
    _make_registry()
    # Force re-init (simulates test teardown / re-setup)
    MetricsRegistry._instance = None
    reg2 = MetricsRegistry.get()
    assert hasattr(reg2, "recorder_reinject_circuit_breaker_drops_total")
    names = set(REGISTRY._names_to_collectors.keys())  # type: ignore[attr-defined]
    assert "recorder_reinject_circuit_breaker_drops_total" in names


@pytest.mark.asyncio
async def test_reinject_circuit_breaker_increments_registry_counter():
    """When batcher hits the reinject circuit breaker the MetricsRegistry counter is incremented."""
    reg = _make_registry()

    # Zero it out by checking current value
    counter = reg.recorder_reinject_circuit_breaker_drops_total

    failing_writer = MagicMock()
    failing_writer.write_columnar = AsyncMock(side_effect=Exception("ch down"))
    failing_writer.write = AsyncMock(side_effect=Exception("ch down"))

    b = Batcher(
        "hft.test_table",
        flush_limit=1,
        writer=failing_writer,
        max_buffer_size=100,
    )
    # Override _reinject_max_failures to 0 so first failure triggers circuit breaker
    b._reinject_max_failures = 0
    b._reinject_consecutive_failures = 1  # already at max

    # Manufacture a flush buffer with one row
    from hft_platform.recorder.batcher import ColumnarBuffer

    flush_buf = ColumnarBuffer("hft.test_table")
    flush_buf.append_row({"col": 1})

    before = counter.labels(table="hft.test_table")._value.get()
    await b._reinject_failed_buffer(flush_buf)
    after = counter.labels(table="hft.test_table")._value.get()

    assert after == before + 1, f"Expected counter to increment by 1 (rows dropped), got {after - before}"
