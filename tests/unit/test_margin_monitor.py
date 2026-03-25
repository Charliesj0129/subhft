"""Tests for MarginMonitor — broker margin utilization monitoring."""

from __future__ import annotations

import pytest

from hft_platform.ops.margin_monitor import MarginMonitor


class FakeBrokerClient:
    """Fake broker client returning configurable margin dicts."""

    def __init__(self, margin_used: int = 0, margin_available: int = 100_000, *, raise_exc: bool = False) -> None:
        self._margin_used = margin_used
        self._margin_available = margin_available
        self._raise_exc = raise_exc

    def get_margin(self) -> dict[str, int]:
        if self._raise_exc:
            msg = "broker connection lost"
            raise ConnectionError(msg)
        return {"margin_used": self._margin_used, "margin_available": self._margin_available}


@pytest.mark.asyncio
async def test_ok_when_below_warn() -> None:
    """Ratio 0.5 (below warn=0.80) should return action='ok'."""
    client = FakeBrokerClient(margin_used=50_000, margin_available=100_000)
    monitor = MarginMonitor(client, warn_ratio=0.80, critical_ratio=0.90, poll_interval_s=0)

    result = await monitor.check(now_ns=1_000_000_000)

    assert result is not None
    assert result.action == "ok"
    assert result.ratio == pytest.approx(0.5)
    assert result.margin_used == 50_000
    assert result.margin_available == 100_000


@pytest.mark.asyncio
async def test_warn_at_threshold() -> None:
    """Ratio 0.85 (above warn=0.80, below critical=0.90) should return action='warn'."""
    client = FakeBrokerClient(margin_used=85_000, margin_available=100_000)
    monitor = MarginMonitor(client, warn_ratio=0.80, critical_ratio=0.90, poll_interval_s=0)

    result = await monitor.check(now_ns=1_000_000_000)

    assert result is not None
    assert result.action == "warn"
    assert result.ratio == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_critical_at_threshold() -> None:
    """Ratio 0.95 (above critical=0.90) should return action='critical'."""
    client = FakeBrokerClient(margin_used=95_000, margin_available=100_000)
    monitor = MarginMonitor(client, warn_ratio=0.80, critical_ratio=0.90, poll_interval_s=0)

    result = await monitor.check(now_ns=1_000_000_000)

    assert result is not None
    assert result.action == "critical"
    assert result.ratio == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_broker_failure_returns_error() -> None:
    """When get_margin() raises, should return action='error'."""
    client = FakeBrokerClient(raise_exc=True)
    monitor = MarginMonitor(client, poll_interval_s=0)

    result = await monitor.check(now_ns=1_000_000_000)

    assert result is not None
    assert result.action == "error"
    assert result.ratio == 0.0


@pytest.mark.asyncio
async def test_respects_poll_interval() -> None:
    """Second call within poll interval should return None."""
    client = FakeBrokerClient(margin_used=50_000, margin_available=100_000)
    monitor = MarginMonitor(client, poll_interval_s=30)

    # First call at t=30s — should poll (elapsed from _last_poll_ns=0 is 30s >= 30s)
    result1 = await monitor.check(now_ns=30_000_000_000)
    assert result1 is not None
    assert result1.action == "ok"

    # Second call at t=40s (only 10s since last poll) — should skip
    result2 = await monitor.check(now_ns=40_000_000_000)
    assert result2 is None

    # Third call at t=61s (31s since last poll, past interval) — should poll again
    result3 = await monitor.check(now_ns=61_000_000_000)
    assert result3 is not None
    assert result3.action == "ok"


@pytest.mark.asyncio
async def test_warn_to_critical_to_ok_transitions() -> None:
    """Verify state transitions: ok -> warn -> critical -> ok with log-once semantics."""
    client = FakeBrokerClient(margin_used=50_000, margin_available=100_000)
    monitor = MarginMonitor(client, warn_ratio=0.80, critical_ratio=0.90, poll_interval_s=0)

    # Start OK
    result = await monitor.check(now_ns=1_000_000_000)
    assert result is not None
    assert result.action == "ok"

    # Move to warn
    client._margin_used = 85_000
    result = await monitor.check(now_ns=2_000_000_000)
    assert result is not None
    assert result.action == "warn"

    # Move to critical
    client._margin_used = 95_000
    result = await monitor.check(now_ns=3_000_000_000)
    assert result is not None
    assert result.action == "critical"

    # Recover to OK
    client._margin_used = 50_000
    result = await monitor.check(now_ns=4_000_000_000)
    assert result is not None
    assert result.action == "ok"


@pytest.mark.asyncio
async def test_zero_available_margin_no_division_by_zero() -> None:
    """When margin_available is 0, should not raise ZeroDivisionError."""
    client = FakeBrokerClient(margin_used=50_000, margin_available=0)
    monitor = MarginMonitor(client, poll_interval_s=0)

    result = await monitor.check(now_ns=1_000_000_000)
    assert result is not None
    # available clamped to 1, so ratio = 50000/1 = 50000.0 -> critical
    assert result.action == "critical"
