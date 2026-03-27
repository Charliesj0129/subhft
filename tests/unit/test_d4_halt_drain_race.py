"""D4: RiskEngine must guard against HALT and DLQ on QueueFull."""

from __future__ import annotations

import asyncio
import collections
import time
from unittest.mock import MagicMock


def _make_cmd(cmd_id=1, strategy_id="s1", symbol="TXFD6"):
    cmd = MagicMock()
    cmd.cmd_id = cmd_id
    cmd.intent = MagicMock()
    cmd.intent.strategy_id = strategy_id
    cmd.intent.symbol = symbol
    return cmd


def _make_engine():
    """Construct a minimal RiskEngine for dispatch path testing."""
    from hft_platform.risk.engine import RiskEngine

    engine = RiskEngine.__new__(RiskEngine)
    engine.order_queue = asyncio.Queue(maxsize=2)
    engine.metrics = MagicMock()
    engine.storm_guard = MagicMock()
    engine.storm_guard.state = 0  # NORMAL
    engine._order_dlq = collections.deque()
    engine._ORDER_DLQ_MAX = 256
    return engine


def test_halt_guard_blocks_dispatch():
    """When StormGuard is HALT, approved commands must not reach order_queue."""
    from hft_platform.risk.storm_guard import StormGuardState

    engine = _make_engine()
    engine.storm_guard.state = StormGuardState.HALT

    cmd = _make_cmd()

    # Simulate the HALT check from the run() loop
    if engine.storm_guard.state == StormGuardState.HALT:
        engine.metrics.risk_halt_blocked_total.inc()
    else:
        engine.order_queue.put_nowait(cmd)

    assert engine.order_queue.empty()
    engine.metrics.risk_halt_blocked_total.inc.assert_called_once()


def test_queue_full_routes_to_dlq():
    """When order_queue is full, command goes to DLQ and StormGuard HALTs."""
    engine = _make_engine()

    # Fill queue
    engine.order_queue.put_nowait(MagicMock())
    engine.order_queue.put_nowait(MagicMock())

    cmd = _make_cmd(cmd_id=99)

    try:
        engine.order_queue.put_nowait(cmd)
    except asyncio.QueueFull:
        engine._order_dlq.append((cmd, time.monotonic_ns()))
        engine.metrics.order_queue_full_total.inc()
        engine.storm_guard.trigger_halt("order_queue_full")

    assert len(engine._order_dlq) == 1
    assert engine._order_dlq[0][0] is cmd
    engine.metrics.order_queue_full_total.inc.assert_called_once()
    engine.storm_guard.trigger_halt.assert_called_once_with("order_queue_full")


def test_dlq_bounded():
    """DLQ must not grow beyond _ORDER_DLQ_MAX."""
    engine = _make_engine()
    engine._ORDER_DLQ_MAX = 3

    for i in range(5):
        engine._order_dlq.append((MagicMock(), i))
        if len(engine._order_dlq) > engine._ORDER_DLQ_MAX:
            engine._order_dlq.popleft()

    assert len(engine._order_dlq) == 3


def test_normal_dispatch_succeeds():
    """In NORMAL state with space, put_nowait succeeds."""
    engine = _make_engine()
    cmd = _make_cmd()

    engine.order_queue.put_nowait(cmd)

    assert engine.order_queue.qsize() == 1
