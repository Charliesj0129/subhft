"""Tests for DeadLetterQueue Prometheus metrics wiring."""

import asyncio
import tempfile

import pytest

from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.order.deadletter import DeadLetterQueue, RejectionReason


@pytest.fixture()
def metrics():
    return MetricsRegistry.get()


@pytest.fixture()
def dlq(tmp_path):
    return DeadLetterQueue(dlq_dir=str(tmp_path), max_buffer_size=100)


def _counter_value(counter_metric) -> float:
    """Read the current value from a prometheus_client Counter label set."""
    return counter_metric._value.get()


@pytest.mark.asyncio
async def test_add_increments_dlq_size_total(dlq, metrics):
    """Each add() call must increment dlq_size_total{source='order'} by 1."""
    labeled = metrics.dlq_size_total.labels(source="order")
    before = _counter_value(labeled)

    await dlq.add(
        order_id="ORD-001",
        strategy_id="strat-a",
        symbol="2330",
        side="BUY",
        price=5000000,  # 500.0000 scaled x10000
        qty=100,
        reason=RejectionReason.CIRCUIT_BREAKER,
        error_message="breaker tripped",
    )

    after = _counter_value(labeled)
    assert after == before + 1.0


@pytest.mark.asyncio
async def test_multiple_adds_accumulate_counter(dlq, metrics):
    """Counter must accumulate correctly across multiple add() calls."""
    labeled = metrics.dlq_size_total.labels(source="order")
    before = _counter_value(labeled)

    for i in range(5):
        await dlq.add(
            order_id=f"ORD-{i:03d}",
            strategy_id="strat-b",
            symbol="0050",
            side="SELL",
            price=1000000,
            qty=10,
            reason=RejectionReason.API_TIMEOUT,
            error_message="timeout",
        )

    after = _counter_value(labeled)
    assert after == before + 5.0
