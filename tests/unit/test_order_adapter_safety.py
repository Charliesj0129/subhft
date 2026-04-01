"""OrderAdapter deadline, retry, and DLQ safety tests.

Tests key safety behaviors of OrderAdapter in isolation:
- Deadline expiry drops stale commands
- Rate limiter rejects when hard cap exceeded
- Circuit breaker rejects when open
- drain_and_cancel empties queue
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.core import timebase
from hft_platform.order.adapter import OrderAdapter
from hft_platform.order.deadletter import DeadLetterQueue


def _make_intent(**overrides) -> OrderIntent:
    """Create a minimal OrderIntent with scaled-int price."""
    defaults = {
        "intent_id": 1,
        "strategy_id": "test_strat",
        "symbol": "2330",
        "intent_type": IntentType.NEW,
        "side": Side.BUY,
        "price": 5950000,  # 595.0 x10000
        "qty": 1,
        "tif": TIF.LIMIT,
    }
    defaults.update(overrides)
    return OrderIntent(**defaults)


def _make_cmd(intent: OrderIntent, deadline_ns: int | None = None, cmd_id: int = 1) -> OrderCommand:
    """Create an OrderCommand with explicit or future deadline."""
    import time

    if deadline_ns is None:
        deadline_ns = time.monotonic_ns() + 5_000_000_000  # 5s in future
    return OrderCommand(
        cmd_id=cmd_id,
        intent=intent,
        deadline_ns=deadline_ns,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=timebase.now_ns(),
    )


def _make_adapter(tmp_path, client=None) -> OrderAdapter:
    """Create an OrderAdapter with minimal YAML config and mocked dependencies."""
    config_file = tmp_path / "order_config.yaml"
    config_file.write_text(
        "rate_limits:\n"
        "  shioaji_soft_cap: 180\n"
        "  shioaji_hard_cap: 250\n"
        "  window_seconds: 10\n"
        "circuit_breaker:\n"
        "  threshold: 5\n"
        "  timeout_seconds: 60\n"
    )
    if client is None:
        client = MagicMock()
        client.place_order = MagicMock(return_value={"id": "T1"})
        client.cancel_order = MagicMock()
        client.get_exchange = MagicMock(return_value="TSE")
    queue = asyncio.Queue()
    adapter = OrderAdapter(str(config_file), queue, client)
    return adapter


# ---------------------------------------------------------------------------
# Test 1: Deadline expiry drops stale commands
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_deadline_expiry_drops_command(tmp_path):
    """A command whose deadline_ns < now_ns must be consumed but NOT dispatched."""
    from unittest.mock import patch

    adapter = _make_adapter(tmp_path)

    # Create command with deadline already in the past
    import time

    expired_deadline = time.monotonic_ns() - 1_000_000  # 1ms ago
    intent = _make_intent()
    cmd = _make_cmd(intent, deadline_ns=expired_deadline)

    # Put expired command in queue
    await adapter.order_queue.put(cmd)

    # Track whether execute is called via patch.object (__slots__ class)
    execute_called = False
    original_execute = adapter.execute

    async def mock_execute(c):
        nonlocal execute_called
        execute_called = True
        await original_execute(c)

    # Run adapter briefly, then stop
    async def stop_after_consume():
        while not adapter.order_queue.empty():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        adapter.running = False

    with patch.object(type(adapter), "execute", side_effect=mock_execute):
        await asyncio.gather(
            adapter.run(),
            stop_after_consume(),
        )

    # Command was consumed (queue is empty) but execute was NOT called
    assert adapter.order_queue.empty()
    assert not execute_called, "Expired command should not trigger execute()"


# ---------------------------------------------------------------------------
# Test 2: Rate limiter rejects when hard cap exceeded -> DLQ
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rate_limiter_rejects_when_exceeded(tmp_path):
    """When rate limiter hard cap is hit, execute() should route intent to DLQ."""
    adapter = _make_adapter(tmp_path)

    # Use a DLQ with tmp_path to avoid polluting the working directory
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path / "dlq"), max_buffer_size=100)
    adapter._dlq = dlq

    # Fill rate limiter past hard cap (250 entries in the window)
    for _ in range(260):
        adapter.rate_limiter.record()

    # Verify rate limiter rejects
    assert not adapter.check_rate_limit(), "Rate limiter should reject after hard cap"

    # Try to execute a command
    intent = _make_intent(intent_id=42)
    cmd = _make_cmd(intent)
    await adapter.execute(cmd)

    # Verify intent landed in DLQ
    stats = await dlq.get_stats()
    assert stats["total_entries"] >= 1, "Rejected order should appear in DLQ"

    # Verify the broker was NOT called
    adapter.client.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: Circuit breaker rejects when open -> DLQ
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_circuit_breaker_rejects_when_open(tmp_path):
    """When circuit breaker is open, execute() should reject and route to DLQ."""
    adapter = _make_adapter(tmp_path)

    # Use a DLQ with tmp_path
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path / "dlq"), max_buffer_size=100)
    adapter._dlq = dlq

    # Force circuit breaker open by recording enough failures
    for _ in range(adapter.circuit_breaker.threshold):
        adapter.circuit_breaker.record_failure()

    assert adapter.circuit_breaker.is_open(), "Circuit breaker should be open after threshold failures"

    # Try to execute a command
    intent = _make_intent(intent_id=99)
    cmd = _make_cmd(intent)
    await adapter.execute(cmd)

    # Verify intent landed in DLQ with circuit_breaker reason
    stats = await dlq.get_stats()
    assert stats["total_entries"] >= 1, "Circuit-breaker-rejected order should appear in DLQ"

    # Verify the broker was NOT called
    adapter.client.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: drain_and_cancel empties the queue
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_drain_and_cancel_empties_queue(tmp_path):
    """drain_and_cancel should remove all pending commands from the queue."""
    adapter = _make_adapter(tmp_path)

    # Put several commands in the queue
    for i in range(5):
        intent = _make_intent(intent_id=i + 1)
        cmd = _make_cmd(intent, cmd_id=i + 1)
        await adapter.order_queue.put(cmd)

    assert adapter.order_queue.qsize() == 5

    # Drain the queue
    cancelled = await adapter.drain_and_cancel()

    # Queue should be empty (no live orders, so cancelled count is 0)
    assert adapter.order_queue.empty(), "Queue should be empty after drain"
    assert cancelled == 0, "No live orders to cancel"


# ---------------------------------------------------------------------------
# Test 5: drain_and_cancel also cancels live orders
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_drain_and_cancel_cancels_live_orders(tmp_path):
    """drain_and_cancel should cancel all live orders via broker client."""
    client = MagicMock()
    client.cancel_order = MagicMock(return_value=None)
    client.place_order = MagicMock(return_value={"id": "T1"})
    client.get_exchange = MagicMock(return_value="TSE")
    adapter = _make_adapter(tmp_path, client=client)

    # Add a pending command
    intent = _make_intent()
    cmd = _make_cmd(intent)
    await adapter.order_queue.put(cmd)

    # Simulate live orders
    adapter.live_orders["test_strat:1"] = {"id": "T1"}
    adapter.live_orders["test_strat:2"] = {"id": "T2"}

    cancelled = await adapter.drain_and_cancel()

    assert adapter.order_queue.empty(), "Queue should be empty after drain"
    assert cancelled == 2, "Both live orders should be cancelled"
    assert client.cancel_order.call_count == 2


# ---------------------------------------------------------------------------
# Test 6: Valid (non-expired) command IS dispatched
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_valid_deadline_command_dispatched(tmp_path):
    """A command with a future deadline should be dispatched via execute()."""
    adapter = _make_adapter(tmp_path)

    # Create command with deadline 5s in the future
    intent = _make_intent()
    cmd = _make_cmd(intent)  # default deadline is 5s ahead

    await adapter.order_queue.put(cmd)

    execute_called = False

    async def mock_execute(c):
        nonlocal execute_called
        execute_called = True

    adapter.execute = mock_execute

    async def stop_after_consume():
        while not adapter.order_queue.empty():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        adapter.running = False

    await asyncio.gather(
        adapter.run(),
        stop_after_consume(),
    )

    assert execute_called, "Valid command should trigger execute()"


# ---------------------------------------------------------------------------
# M1: Live StormGuard HALT check in execute()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_stormguard_halt_rejects_even_if_cmd_stamped_normal(tmp_path):
    """execute() should reject orders when live StormGuard is HALT, even if
    cmd.storm_guard_state was stamped NORMAL at RiskEngine time (TOCTOU fix)."""
    adapter = _make_adapter(tmp_path)

    # Simulate live StormGuard in HALT state
    mock_sg = MagicMock()
    mock_sg.state = StormGuardState.HALT
    adapter._storm_guard = mock_sg

    intent = _make_intent()
    cmd = _make_cmd(intent)  # storm_guard_state = NORMAL (stamped at creation)
    assert cmd.storm_guard_state == StormGuardState.NORMAL

    dlq_size_before = len(adapter._dlq._buffer) if hasattr(adapter._dlq, "_buffer") else 0
    await adapter.execute(cmd)

    # Should have been DLQ'd, not dispatched
    dlq_size_after = len(adapter._dlq._buffer) if hasattr(adapter._dlq, "_buffer") else 0
    assert dlq_size_after > dlq_size_before, "Order should have been added to DLQ"


@pytest.mark.asyncio
async def test_live_stormguard_halt_allows_halt_flatten(tmp_path):
    """halt_flatten orders should pass even when live StormGuard is HALT."""
    adapter = _make_adapter(tmp_path)

    mock_sg = MagicMock()
    mock_sg.state = StormGuardState.HALT
    adapter._storm_guard = mock_sg

    intent = _make_intent(reason="halt_flatten")
    cmd = _make_cmd(intent)

    dlq_size_before = len(adapter._dlq._buffer) if hasattr(adapter._dlq, "_buffer") else 0
    # Mock the dispatch path to prevent actual broker call
    from unittest.mock import AsyncMock
    adapter._enqueue_api = AsyncMock()
    await adapter.execute(cmd)

    # Should NOT be DLQ'd
    dlq_size_after = len(adapter._dlq._buffer) if hasattr(adapter._dlq, "_buffer") else 0
    assert dlq_size_after == dlq_size_before, "halt_flatten should NOT be DLQ'd"


# ---------------------------------------------------------------------------
# E-6: CANCEL/FORCE_FLAT bypass per-symbol rate limiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_bypasses_per_symbol_rate_limiter(tmp_path):
    """CANCEL intents must not be blocked by per-symbol rate limiter (E-6 fix)."""
    from unittest.mock import AsyncMock

    adapter = _make_adapter(tmp_path)

    # Force per-symbol rate limiter to reject everything
    from hft_platform.core.rate_limiter import PerSymbolRateResult

    mock_ps_limiter = MagicMock()
    mock_ps_limiter.check = MagicMock(return_value=PerSymbolRateResult.HARD)
    adapter.per_symbol_rate_limiter = mock_ps_limiter
    adapter._enqueue_api = AsyncMock()

    intent = _make_intent(intent_type=IntentType.CANCEL, symbol="")
    cmd = _make_cmd(intent)

    dlq_before = len(adapter._dlq._buffer) if hasattr(adapter._dlq, "_buffer") else 0
    await adapter.execute(cmd)
    dlq_after = len(adapter._dlq._buffer) if hasattr(adapter._dlq, "_buffer") else 0

    assert dlq_after == dlq_before, "CANCEL must not be rate-limited to DLQ"
    # per_symbol check should not even be called for CANCEL
    mock_ps_limiter.check.assert_not_called()


@pytest.mark.asyncio
async def test_force_flat_bypasses_per_symbol_rate_limiter(tmp_path):
    """FORCE_FLAT intents must not be blocked by per-symbol rate limiter (E-6 fix)."""
    from unittest.mock import AsyncMock

    adapter = _make_adapter(tmp_path)

    from hft_platform.core.rate_limiter import PerSymbolRateResult

    mock_ps_limiter = MagicMock()
    mock_ps_limiter.check = MagicMock(return_value=PerSymbolRateResult.HARD)
    adapter.per_symbol_rate_limiter = mock_ps_limiter
    adapter._enqueue_api = AsyncMock()

    intent = _make_intent(intent_type=IntentType.FORCE_FLAT)
    cmd = _make_cmd(intent)

    dlq_before = len(adapter._dlq._buffer) if hasattr(adapter._dlq, "_buffer") else 0
    await adapter.execute(cmd)
    dlq_after = len(adapter._dlq._buffer) if hasattr(adapter._dlq, "_buffer") else 0

    assert dlq_after == dlq_before, "FORCE_FLAT must not be rate-limited to DLQ"
    mock_ps_limiter.check.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_bypasses_circuit_breaker(tmp_path):
    """CANCEL intents must not be blocked by circuit breaker (E-6 fix)."""
    from unittest.mock import AsyncMock

    adapter = _make_adapter(tmp_path)
    adapter._enqueue_api = AsyncMock()

    # Force circuit breaker open
    for _ in range(adapter.circuit_breaker.threshold):
        adapter.circuit_breaker.record_failure()
    assert adapter.circuit_breaker.is_open()

    intent = _make_intent(intent_type=IntentType.CANCEL)
    cmd = _make_cmd(intent)

    dlq_before = len(adapter._dlq._buffer) if hasattr(adapter._dlq, "_buffer") else 0
    await adapter.execute(cmd)
    dlq_after = len(adapter._dlq._buffer) if hasattr(adapter._dlq, "_buffer") else 0

    assert dlq_after == dlq_before, "CANCEL must bypass circuit breaker"


@pytest.mark.asyncio
async def test_cancel_bypasses_global_rate_limiter(tmp_path):
    """CANCEL intents must not be blocked by global rate limiter (E-6 fix)."""
    from unittest.mock import AsyncMock

    adapter = _make_adapter(tmp_path)
    adapter._enqueue_api = AsyncMock()

    # Fill global rate limiter past hard cap
    for _ in range(260):
        adapter.rate_limiter.record()
    assert not adapter.check_rate_limit()

    intent = _make_intent(intent_type=IntentType.CANCEL)
    cmd = _make_cmd(intent)

    dlq_before = len(adapter._dlq._buffer) if hasattr(adapter._dlq, "_buffer") else 0
    await adapter.execute(cmd)
    dlq_after = len(adapter._dlq._buffer) if hasattr(adapter._dlq, "_buffer") else 0

    assert dlq_after == dlq_before, "CANCEL must bypass global rate limiter"


@pytest.mark.asyncio
async def test_new_order_still_rejected_by_rate_limiter(tmp_path):
    """NEW intents must still be blocked by rate limiter (regression guard)."""
    adapter = _make_adapter(tmp_path)
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path / "dlq"), max_buffer_size=100)
    adapter._dlq = dlq

    for _ in range(260):
        adapter.rate_limiter.record()

    intent = _make_intent(intent_type=IntentType.NEW)
    cmd = _make_cmd(intent)
    await adapter.execute(cmd)

    stats = await dlq.get_stats()
    assert stats["total_entries"] >= 1, "NEW orders must still be rate-limited"


# ---------------------------------------------------------------------------
# I-4: _api_worker HALT gate — skip non-exempt orders in HALT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_worker_skips_new_orders_during_halt(tmp_path):
    """_api_worker must skip NEW orders when StormGuard is HALT (I-4 fix)."""
    adapter = _make_adapter(tmp_path)
    adapter.running = True

    mock_sg = MagicMock()
    mock_sg.state = StormGuardState.HALT
    adapter._storm_guard = mock_sg

    dispatched = []

    async def mock_dispatch(cmd):
        dispatched.append(cmd)

    adapter._dispatch_to_api = mock_dispatch

    intent = _make_intent(intent_type=IntentType.NEW)
    cmd = _make_cmd(intent)
    await adapter._api_queue.put(cmd)

    # Run worker as a task and cancel after processing
    task = asyncio.create_task(adapter._api_worker())
    await asyncio.sleep(0.1)
    adapter.running = False
    task.cancel()
    await asyncio.wait_for(task, timeout=1.0)

    assert len(dispatched) == 0, "NEW orders must be skipped during HALT in _api_worker"


@pytest.mark.asyncio
async def test_api_worker_allows_cancel_during_halt(tmp_path):
    """_api_worker must still dispatch CANCEL orders during HALT (I-4 fix)."""
    adapter = _make_adapter(tmp_path)
    adapter.running = True

    mock_sg = MagicMock()
    mock_sg.state = StormGuardState.HALT
    adapter._storm_guard = mock_sg

    dispatched = []

    async def mock_dispatch(cmd):
        dispatched.append(cmd)

    adapter._dispatch_to_api = mock_dispatch

    intent = _make_intent(intent_type=IntentType.CANCEL)
    cmd = _make_cmd(intent)
    await adapter._api_queue.put(cmd)

    task = asyncio.create_task(adapter._api_worker())
    await asyncio.sleep(0.1)
    adapter.running = False
    task.cancel()
    await asyncio.wait_for(task, timeout=1.0)

    assert len(dispatched) == 1, "CANCEL orders must be dispatched even during HALT"


@pytest.mark.asyncio
async def test_api_worker_allows_force_flat_during_halt(tmp_path):
    """_api_worker must still dispatch FORCE_FLAT orders during HALT (I-4 fix)."""
    adapter = _make_adapter(tmp_path)
    adapter.running = True

    mock_sg = MagicMock()
    mock_sg.state = StormGuardState.HALT
    adapter._storm_guard = mock_sg

    dispatched = []

    async def mock_dispatch(cmd):
        dispatched.append(cmd)

    adapter._dispatch_to_api = mock_dispatch

    intent = _make_intent(intent_type=IntentType.FORCE_FLAT)
    cmd = _make_cmd(intent)
    await adapter._api_queue.put(cmd)

    task = asyncio.create_task(adapter._api_worker())
    await asyncio.sleep(0.1)
    adapter.running = False
    task.cancel()
    await asyncio.wait_for(task, timeout=1.0)

    assert len(dispatched) == 1, "FORCE_FLAT orders must be dispatched even during HALT"
