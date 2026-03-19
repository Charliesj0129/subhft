"""WU-18: Integration tests for order-risk-halt pipeline.

Tests the full flow: OrderIntent -> RiskEngine -> OrderAdapter
with real asyncio queues and a mock broker client.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

import pytest
import yaml

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.core import timebase
from hft_platform.risk.engine import RiskEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_risk_config(
    *,
    halt_threshold: int = -1_000_000,
    storm_threshold: int = -500_000,
    warm_threshold: int = -200_000,
    max_price_cap: float = 5000.0,
    max_notional: int = 10_000_000,
    max_order_size: int = 1000,
) -> dict[str, Any]:
    return {
        "global_defaults": {
            "max_price_cap": max_price_cap,
            "tick_size": 0.01,
            "price_band_ticks": 20,
            "max_notional": max_notional,
            "max_qty": max_order_size,
        },
        "risk": {
            "max_order_size": max_order_size,
        },
        "storm_guard": {
            "warm_threshold": warm_threshold,
            "storm_threshold": storm_threshold,
            "halt_threshold": halt_threshold,
        },
        "strategies": {},
    }


def _make_adapter_config(
    *,
    soft_cap: int = 180,
    hard_cap: int = 250,
    window_seconds: int = 10,
    cb_threshold: int = 5,
    cb_timeout: int = 60,
) -> dict[str, Any]:
    return {
        "rate_limits": {
            "shioaji_soft_cap": soft_cap,
            "shioaji_hard_cap": hard_cap,
            "window_seconds": window_seconds,
        },
        "circuit_breaker": {
            "threshold": cb_threshold,
            "timeout_seconds": cb_timeout,
        },
    }


def _write_yaml(data: dict[str, Any], suffix: str = ".yaml") -> str:
    """Write config dict to a temp file and return the path."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(data, f)
    return path


def _make_intent(
    intent_id: int = 1,
    strategy_id: str = "test_strat",
    symbol: str = "2330",
    intent_type: IntentType = IntentType.NEW,
    side: Side = Side.BUY,
    price: int = 100_0000,  # 100.0 scaled x10000
    qty: int = 10,
    tif: TIF = TIF.LIMIT,
    target_order_id: str | None = None,
) -> OrderIntent:
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
    """Minimal mock broker satisfying OrderAdapter client validation."""

    def __init__(self) -> None:
        self.placed: list[dict[str, Any]] = []
        self.cancelled: list[Any] = []

    def place_order(self, **kwargs: Any) -> dict[str, Any]:
        trade = {"order_id": f"B{len(self.placed)+1}", **kwargs}
        self.placed.append(trade)
        return trade

    def cancel_order(self, trade: Any, **kwargs: Any) -> dict[str, Any]:
        self.cancelled.append(trade)
        return {"status": "cancelled"}

    def get_exchange(self, symbol: str) -> str:
        return "TSE"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def risk_config_path() -> str:
    path = _write_yaml(_make_risk_config())
    yield path
    os.unlink(path)


@pytest.fixture()
def adapter_config_path() -> str:
    path = _write_yaml(_make_adapter_config())
    yield path
    os.unlink(path)


@pytest.fixture()
def queues() -> tuple[asyncio.Queue, asyncio.Queue]:
    intent_q: asyncio.Queue = asyncio.Queue(maxsize=64)
    order_q: asyncio.Queue = asyncio.Queue(maxsize=64)
    return intent_q, order_q


@pytest.fixture()
def mock_broker() -> MockBroker:
    return MockBroker()


@pytest.fixture()
def risk_engine(risk_config_path: str, queues: tuple) -> RiskEngine:
    intent_q, order_q = queues
    return RiskEngine(risk_config_path, intent_q, order_q)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_new_flow(risk_engine: RiskEngine, queues: tuple) -> None:
    """1. Normal NEW intent passes risk and arrives in order_queue."""
    intent_q, order_q = queues
    intent = _make_intent()

    await intent_q.put(intent)

    # Run risk engine for one iteration
    task = asyncio.create_task(risk_engine.run())
    try:
        cmd: OrderCommand = await asyncio.wait_for(order_q.get(), timeout=2.0)
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
async def test_cancel_flow(risk_engine: RiskEngine, queues: tuple) -> None:
    """2. CANCEL intent passes risk normally."""
    intent_q, order_q = queues
    intent = _make_intent(
        intent_type=IntentType.CANCEL,
        target_order_id="prev_1",
        price=0,
    )

    await intent_q.put(intent)
    task = asyncio.create_task(risk_engine.run())
    try:
        cmd: OrderCommand = await asyncio.wait_for(order_q.get(), timeout=2.0)
        assert cmd.intent.intent_type == IntentType.CANCEL
    finally:
        risk_engine.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_halt_blocks_new(risk_engine: RiskEngine, queues: tuple) -> None:
    """3. HALT state blocks NEW intents."""
    # Trigger HALT via StormGuardFSM
    risk_engine.storm_guard.update_pnl(-2_000_000)
    assert risk_engine.storm_guard.state == StormGuardState.HALT

    intent = _make_intent()
    decision = risk_engine.evaluate(intent)
    assert not decision.approved
    assert "HALT" in decision.reason_code


@pytest.mark.asyncio
async def test_halt_allows_cancel(risk_engine: RiskEngine, queues: tuple) -> None:
    """4. HALT state still allows CANCEL intents."""
    risk_engine.storm_guard.update_pnl(-2_000_000)
    assert risk_engine.storm_guard.state == StormGuardState.HALT

    cancel_intent = _make_intent(
        intent_type=IntentType.CANCEL,
        target_order_id="existing_1",
        price=0,
    )
    decision = risk_engine.evaluate(cancel_intent)
    assert decision.approved


@pytest.mark.asyncio
async def test_expired_deadline_skipped(
    adapter_config_path: str,
    queues: tuple,
    mock_broker: MockBroker,
) -> None:
    """5. OrderAdapter skips commands with expired deadlines."""
    from hft_platform.order.adapter import OrderAdapter

    _, order_q = queues
    adapter = OrderAdapter(adapter_config_path, order_q, mock_broker)

    # Create a command with an already-expired deadline
    intent = _make_intent()
    cmd = OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=1,  # nanosecond 1 -- long expired
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=1,
    )
    await order_q.put(cmd)

    task = asyncio.create_task(adapter.run())
    try:
        # Give the adapter time to process
        await asyncio.sleep(0.3)
        # Broker should NOT have been called
        assert len(mock_broker.placed) == 0
    finally:
        adapter.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_circuit_breaker_rejection(
    adapter_config_path: str,
    queues: tuple,
    mock_broker: MockBroker,
) -> None:
    """6. Circuit breaker rejects orders when open."""
    from hft_platform.order.adapter import OrderAdapter

    _, order_q = queues
    adapter = OrderAdapter(adapter_config_path, order_q, mock_broker)

    # Force circuit breaker open by recording enough failures
    for _ in range(adapter.circuit_breaker.threshold + 1):
        adapter.circuit_breaker.record_failure()
    assert adapter.circuit_breaker.is_open()

    intent = _make_intent()
    cmd = OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=timebase.now_ns() + 5_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=timebase.now_ns(),
    )
    # Execute directly (bypasses run loop for isolation)
    await adapter.execute(cmd)
    assert len(mock_broker.placed) == 0


@pytest.mark.asyncio
async def test_rate_limit_rejection(
    adapter_config_path: str,
    queues: tuple,
    mock_broker: MockBroker,
) -> None:
    """7. Rate limiter rejects orders when exhausted."""
    from hft_platform.order.adapter import OrderAdapter

    _, order_q = queues
    # Create adapter with very tight rate limits
    tight_cfg = _make_adapter_config(soft_cap=1, hard_cap=2, window_seconds=60)
    tight_path = _write_yaml(tight_cfg)
    try:
        adapter = OrderAdapter(tight_path, order_q, mock_broker)

        # Exhaust the rate limiter
        for _ in range(adapter.rate_limiter.hard_cap + 1):
            adapter.rate_limiter.record()

        intent = _make_intent()
        cmd = OrderCommand(
            cmd_id=1,
            intent=intent,
            deadline_ns=timebase.now_ns() + 5_000_000_000,
            storm_guard_state=StormGuardState.NORMAL,
            created_ns=timebase.now_ns(),
        )
        await adapter.execute(cmd)
        assert len(mock_broker.placed) == 0
    finally:
        os.unlink(tight_path)


@pytest.mark.asyncio
async def test_dlq_population(
    adapter_config_path: str,
    queues: tuple,
    mock_broker: MockBroker,
) -> None:
    """8. Rejected orders are added to the dead letter queue."""
    from hft_platform.order.adapter import OrderAdapter

    _, order_q = queues
    adapter = OrderAdapter(adapter_config_path, order_q, mock_broker)

    # Force circuit breaker open
    for _ in range(adapter.circuit_breaker.threshold + 1):
        adapter.circuit_breaker.record_failure()

    initial_size = len(adapter._dlq._buffer)

    intent = _make_intent()
    cmd = OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=timebase.now_ns() + 5_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=timebase.now_ns(),
    )
    await adapter.execute(cmd)

    assert len(adapter._dlq._buffer) > initial_size
