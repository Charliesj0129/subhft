"""WU-18: Integration tests for order-risk-halt pipeline."""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest
import yaml

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side, StormGuardState
from hft_platform.core import timebase
from hft_platform.feed_adapter.shioaji.order_codec import ShioajiOrderCodec
from hft_platform.risk.engine import RiskEngine


def _test_codec() -> ShioajiOrderCodec:
    return ShioajiOrderCodec()


def _make_risk_config(
    *,
    halt_threshold=-1_000_000,
    storm_threshold=-500_000,
    warm_threshold=-200_000,
    max_price_cap=5000.0,
    max_notional=10_000_000,
    max_order_size=1000,
):
    return {
        "global_defaults": {
            "max_price_cap": max_price_cap,
            "tick_size": 0.01,
            "price_band_ticks": 20,
            "max_notional": max_notional,
            "max_qty": max_order_size,
        },
        "risk": {"max_order_size": max_order_size},
        "storm_guard": {
            "warm_threshold": warm_threshold,
            "storm_threshold": storm_threshold,
            "halt_threshold": halt_threshold,
        },
        "strategies": {},
    }


def _make_adapter_config(*, soft_cap=180, hard_cap=250, window_seconds=10, cb_threshold=5, cb_timeout=60):
    return {
        "rate_limits": {"shioaji_soft_cap": soft_cap, "shioaji_hard_cap": hard_cap, "window_seconds": window_seconds},
        "circuit_breaker": {"threshold": cb_threshold, "timeout_seconds": cb_timeout},
    }


def _write_yaml(data, suffix=".yaml"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(data, f)
    return path


def _make_intent(
    intent_id=1,
    strategy_id="test_strat",
    symbol="2330",
    intent_type=IntentType.NEW,
    side=Side.BUY,
    price=100_0000,
    qty=10,
    tif=TIF.LIMIT,
    target_order_id=None,
):
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
        tif=tif,
        target_order_id=target_order_id,
        timestamp_ns=timebase.now_ns(),
    )


class MockBroker:
    def __init__(self):
        self.placed, self.cancelled = [], []

    def place_order(self, **kw):
        t = {"order_id": f"B{len(self.placed) + 1}", **kw}
        self.placed.append(t)
        return t

    def cancel_order(self, trade, **kw):
        self.cancelled.append(trade)
        return {"status": "cancelled"}

    def get_exchange(self, symbol):
        return "TSE"


@pytest.fixture()
def risk_config_path():
    path = _write_yaml(_make_risk_config())
    yield path
    os.unlink(path)


@pytest.fixture()
def adapter_config_path():
    path = _write_yaml(_make_adapter_config())
    yield path
    os.unlink(path)


@pytest.fixture()
def queues():
    return asyncio.Queue(maxsize=64), asyncio.Queue(maxsize=64)


@pytest.fixture()
def mock_broker():
    return MockBroker()


@pytest.fixture()
def risk_engine(risk_config_path, queues):
    return RiskEngine(risk_config_path, queues[0], queues[1])


@pytest.mark.asyncio
async def test_normal_new_flow(risk_engine, queues):
    intent_q, order_q = queues
    intent = _make_intent()
    await intent_q.put(intent)
    task = asyncio.create_task(risk_engine.run())
    try:
        cmd = await asyncio.wait_for(order_q.get(), timeout=2.0)
        assert cmd.intent is intent
        assert cmd.storm_guard_state == StormGuardState.NORMAL
        assert cmd.deadline_ns > 0
        assert cmd.cmd_id > 0
    finally:
        risk_engine.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_cancel_flow(risk_engine, queues):
    intent_q, order_q = queues
    intent = _make_intent(intent_type=IntentType.CANCEL, target_order_id="prev_1", price=0)
    await intent_q.put(intent)
    task = asyncio.create_task(risk_engine.run())
    try:
        cmd = await asyncio.wait_for(order_q.get(), timeout=2.0)
        assert cmd.intent.intent_type == IntentType.CANCEL
    finally:
        risk_engine.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_halt_blocks_new(risk_engine, queues):
    risk_engine.storm_guard.trigger_halt("test")
    assert risk_engine.storm_guard.state == StormGuardState.HALT
    decision = risk_engine.evaluate(_make_intent())
    assert not decision.approved
    assert "HALT" in decision.reason_code


@pytest.mark.asyncio
async def test_halt_allows_cancel(risk_engine, queues):
    risk_engine.storm_guard.trigger_halt("test")
    assert risk_engine.storm_guard.state == StormGuardState.HALT
    decision = risk_engine.evaluate(_make_intent(intent_type=IntentType.CANCEL, target_order_id="x", price=0))
    assert decision.approved


@pytest.mark.asyncio
async def test_expired_deadline_skipped(adapter_config_path, queues, mock_broker):
    from hft_platform.order.adapter import OrderAdapter

    _, order_q = queues
    adapter = OrderAdapter(adapter_config_path, order_q, mock_broker, broker_codec=_test_codec())
    cmd = OrderCommand(
        cmd_id=1, intent=_make_intent(), deadline_ns=1, storm_guard_state=StormGuardState.NORMAL, created_ns=1
    )
    await order_q.put(cmd)
    task = asyncio.create_task(adapter.run())
    try:
        await asyncio.sleep(0.3)
        assert len(mock_broker.placed) == 0
    finally:
        adapter.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_circuit_breaker_rejection(adapter_config_path, queues, mock_broker):
    from hft_platform.order.adapter import OrderAdapter

    _, order_q = queues
    adapter = OrderAdapter(adapter_config_path, order_q, mock_broker, broker_codec=_test_codec())
    for _ in range(adapter.circuit_breaker.threshold + 1):
        adapter.circuit_breaker.record_failure()
    assert adapter.circuit_breaker.is_open()
    cmd = OrderCommand(
        cmd_id=1,
        intent=_make_intent(),
        deadline_ns=timebase.now_ns() + 5_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=timebase.now_ns(),
    )
    await adapter.execute(cmd)
    assert len(mock_broker.placed) == 0


@pytest.mark.asyncio
async def test_rate_limit_rejection(adapter_config_path, queues, mock_broker):
    from hft_platform.order.adapter import OrderAdapter

    _, order_q = queues
    tight_path = _write_yaml(_make_adapter_config(soft_cap=1, hard_cap=2, window_seconds=60))
    try:
        adapter = OrderAdapter(tight_path, order_q, mock_broker, broker_codec=_test_codec())
        for _ in range(adapter.rate_limiter.hard_cap + 1):
            adapter.rate_limiter.record()
        cmd = OrderCommand(
            cmd_id=1,
            intent=_make_intent(),
            deadline_ns=timebase.now_ns() + 5_000_000_000,
            storm_guard_state=StormGuardState.NORMAL,
            created_ns=timebase.now_ns(),
        )
        await adapter.execute(cmd)
        assert len(mock_broker.placed) == 0
    finally:
        os.unlink(tight_path)


@pytest.mark.asyncio
async def test_dlq_population(adapter_config_path, queues, mock_broker):
    from hft_platform.order.adapter import OrderAdapter

    _, order_q = queues
    adapter = OrderAdapter(adapter_config_path, order_q, mock_broker, broker_codec=_test_codec())
    for _ in range(adapter.circuit_breaker.threshold + 1):
        adapter.circuit_breaker.record_failure()
    initial_size = len(adapter._dlq._buffer)
    cmd = OrderCommand(
        cmd_id=1,
        intent=_make_intent(),
        deadline_ns=timebase.now_ns() + 5_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=timebase.now_ns(),
    )
    await adapter.execute(cmd)
    assert len(adapter._dlq._buffer) > initial_size
