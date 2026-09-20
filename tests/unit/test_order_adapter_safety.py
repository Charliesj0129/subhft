"""OrderAdapter deadline, retry, and refusal safety tests.

Tests key safety behaviors of OrderAdapter in isolation:
- Deadline expiry drops stale commands
- Rate limiter rejects when hard cap exceeded
- Circuit breaker rejects when open
- drain_and_cancel empties queue

A guard that refuses an intent before any broker call books it on
``order_local_reject_total{reason=...}``, not on the dead-letter queue: nothing
left the platform, so there is nothing to replay. The refusal assertions below
therefore read that counter, and several of them additionally pin that the DLQ
stayed empty -- which is the contract, not an accident.
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
    adapter.shadow_sink.enabled = False
    return adapter


def _local_rejects(adapter: OrderAdapter, reason: str) -> float:
    """Pre-dispatch refusals booked under ``reason`` so far, process-wide.

    The Prometheus registry outlives a single test, so every caller compares a
    delta rather than an absolute.
    """
    return adapter.metrics.order_local_reject_total.labels(reason=reason)._value.get()


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
# Test 2: Rate limiter rejects when hard cap exceeded
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rate_limiter_rejects_when_exceeded(tmp_path):
    """When rate limiter hard cap is hit, execute() must refuse the intent."""
    adapter = _make_adapter(tmp_path)

    # Use a DLQ with tmp_path to avoid polluting the working directory
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path / "dlq"), max_buffer_size=100)
    adapter._dlq = dlq
    rejects_before = _local_rejects(adapter, "rate_limit")

    # Fill rate limiter past hard cap (250 entries in the window)
    for _ in range(260):
        adapter.rate_limiter.record()

    # Verify rate limiter rejects
    assert not adapter.check_rate_limit(), "Rate limiter should reject after hard cap"

    # Try to execute a command
    intent = _make_intent(intent_id=42)
    cmd = _make_cmd(intent)
    await adapter.execute(cmd)

    # Verify the refusal was booked under its own reason
    assert _local_rejects(adapter, "rate_limit") == rejects_before + 1

    # ...and NOT as a dead letter: nothing reached the broker, so there is
    # nothing to replay and OrderDeadLetterQueueGrowing must not fire.
    stats = await dlq.get_stats()
    assert stats["total_entries"] == 0, "a refusal is not a dead letter"

    # Verify the broker was NOT called
    adapter.client.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: Circuit breaker rejects when open
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_circuit_breaker_rejects_when_open(tmp_path):
    """When circuit breaker is open, execute() must refuse the intent.

    This is the 2026-09-16 shape: an open breaker rejected 1,733 intents over
    three weeks, every one of them counted as a dead letter.
    """
    adapter = _make_adapter(tmp_path)

    # Use a DLQ with tmp_path
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path / "dlq"), max_buffer_size=100)
    adapter._dlq = dlq
    rejects_before = _local_rejects(adapter, "circuit_breaker")

    # Force circuit breaker open by recording enough failures
    for _ in range(adapter.circuit_breaker.threshold):
        adapter.circuit_breaker.record_failure()

    assert adapter.circuit_breaker.is_open(), "Circuit breaker should be open after threshold failures"

    # Try to execute a command
    intent = _make_intent(intent_id=99)
    cmd = _make_cmd(intent)
    await adapter.execute(cmd)

    # Verify the refusal was booked under circuit_breaker, not dead-lettered
    assert _local_rejects(adapter, "circuit_breaker") == rejects_before + 1
    stats = await dlq.get_stats()
    assert stats["total_entries"] == 0, "an open breaker is a refusal, not a dead letter"

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

    # Simulate live StormGuard in HALT state.
    #
    # ``_intent_reduces_position`` must be stubbed explicitly: the adapter
    # calls ``bool(sg._intent_reduces_position(intent))``, and a bare
    # MagicMock returns a truthy MagicMock, which makes every intent look
    # like a reducing cover order and exempt from HALT. Until 2026-09-20 this
    # test asserted only "the DLQ grew", and it grew from the unrelated
    # broker_codec_missing rejection further down execute() -- so it passed
    # while proving nothing about HALT.
    mock_sg = MagicMock()
    mock_sg.state = StormGuardState.HALT
    mock_sg.is_halt_exempt.return_value = False
    mock_sg._intent_reduces_position = MagicMock(return_value=False)
    adapter._storm_guard = mock_sg

    intent = _make_intent()
    cmd = _make_cmd(intent)  # storm_guard_state = NORMAL (stamped at creation)
    assert cmd.storm_guard_state == StormGuardState.NORMAL

    rejects_before = _local_rejects(adapter, "stormguard_halt")
    await adapter.execute(cmd)

    # Should have been refused, not dispatched
    assert _local_rejects(adapter, "stormguard_halt") == rejects_before + 1
    adapter.client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_live_stormguard_halt_allows_halt_flatten(tmp_path):
    """halt_flatten orders should pass even when live StormGuard is HALT."""
    adapter = _make_adapter(tmp_path)

    mock_sg = MagicMock()
    mock_sg.state = StormGuardState.HALT
    mock_sg.is_halt_exempt.return_value = False
    # Pinned so the pass comes from FORCE_FLAT's own exemption, not from a
    # MagicMock answering "yes, this reduces the position".
    mock_sg._intent_reduces_position = MagicMock(return_value=False)
    adapter._storm_guard = mock_sg

    intent = _make_intent(intent_type=IntentType.FORCE_FLAT, reason="halt_flatten")
    cmd = _make_cmd(intent)

    rejects_before = _local_rejects(adapter, "stormguard_halt")
    # Mock the dispatch path to prevent actual broker call
    from unittest.mock import AsyncMock

    adapter.running = True
    adapter._enqueue_api = AsyncMock(return_value=True)
    await adapter.execute(cmd)

    # Not refused, and it actually reached dispatch -- asserting only "no
    # refusal" would pass even if the intent silently vanished.
    assert _local_rejects(adapter, "stormguard_halt") == rejects_before
    adapter._enqueue_api.assert_awaited_once()


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
    adapter.running = True
    adapter._enqueue_api = AsyncMock(return_value=True)

    intent = _make_intent(intent_type=IntentType.CANCEL, symbol="")
    cmd = _make_cmd(intent)

    rejects_before = _local_rejects(adapter, "rate_limit")
    await adapter.execute(cmd)

    assert _local_rejects(adapter, "rate_limit") == rejects_before, "CANCEL must not be rate-limited"
    adapter._enqueue_api.assert_awaited_once()
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
    adapter.running = True
    adapter._enqueue_api = AsyncMock(return_value=True)

    intent = _make_intent(intent_type=IntentType.FORCE_FLAT)
    cmd = _make_cmd(intent)

    rejects_before = _local_rejects(adapter, "rate_limit")
    await adapter.execute(cmd)

    assert _local_rejects(adapter, "rate_limit") == rejects_before, "FORCE_FLAT must not be rate-limited"
    adapter._enqueue_api.assert_awaited_once()
    mock_ps_limiter.check.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_bypasses_circuit_breaker(tmp_path):
    """CANCEL intents must not be blocked by circuit breaker (E-6 fix)."""
    from unittest.mock import AsyncMock

    adapter = _make_adapter(tmp_path)
    adapter.running = True
    adapter._enqueue_api = AsyncMock(return_value=True)

    # Force circuit breaker open
    for _ in range(adapter.circuit_breaker.threshold):
        adapter.circuit_breaker.record_failure()
    assert adapter.circuit_breaker.is_open()

    intent = _make_intent(intent_type=IntentType.CANCEL)
    cmd = _make_cmd(intent)

    rejects_before = _local_rejects(adapter, "circuit_breaker")
    await adapter.execute(cmd)

    assert _local_rejects(adapter, "circuit_breaker") == rejects_before, "CANCEL must bypass circuit breaker"
    adapter._enqueue_api.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_bypasses_global_rate_limiter(tmp_path):
    """CANCEL intents must not be blocked by global rate limiter (E-6 fix)."""
    from unittest.mock import AsyncMock

    adapter = _make_adapter(tmp_path)
    adapter.running = True
    adapter._enqueue_api = AsyncMock(return_value=True)

    # Fill global rate limiter past hard cap
    for _ in range(260):
        adapter.rate_limiter.record()
    assert not adapter.check_rate_limit()

    intent = _make_intent(intent_type=IntentType.CANCEL)
    cmd = _make_cmd(intent)

    rejects_before = _local_rejects(adapter, "rate_limit")
    await adapter.execute(cmd)

    assert _local_rejects(adapter, "rate_limit") == rejects_before, "CANCEL must bypass global rate limiter"
    adapter._enqueue_api.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_order_still_rejected_by_rate_limiter(tmp_path):
    """NEW intents must still be blocked by rate limiter (regression guard)."""
    adapter = _make_adapter(tmp_path)
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path / "dlq"), max_buffer_size=100)
    adapter._dlq = dlq

    for _ in range(260):
        adapter.rate_limiter.record()

    rejects_before = _local_rejects(adapter, "rate_limit")
    intent = _make_intent(intent_type=IntentType.NEW)
    cmd = _make_cmd(intent)
    await adapter.execute(cmd)

    assert _local_rejects(adapter, "rate_limit") == rejects_before + 1, "NEW orders must still be rate-limited"
    adapter.client.place_order.assert_not_called()


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
    mock_sg.is_halt_exempt.return_value = False
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
    # P1-4 follow-up: ``_api_worker`` now re-raises ``CancelledError`` after
    # cleanup so cooperative cancellation propagates upward.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)

    assert len(dispatched) == 0, "NEW orders must be skipped during HALT in _api_worker"


@pytest.mark.asyncio
async def test_api_worker_allows_cancel_during_halt(tmp_path):
    """_api_worker must still dispatch CANCEL orders during HALT (I-4 fix)."""
    adapter = _make_adapter(tmp_path)
    adapter.running = True

    mock_sg = MagicMock()
    mock_sg.state = StormGuardState.HALT
    mock_sg.is_halt_exempt.return_value = False
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
    # P1-4 follow-up: ``_api_worker`` now re-raises ``CancelledError`` after
    # cleanup so cooperative cancellation propagates upward.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)

    assert len(dispatched) == 1, "CANCEL orders must be dispatched even during HALT"


@pytest.mark.asyncio
async def test_api_worker_allows_force_flat_during_halt(tmp_path):
    """_api_worker must still dispatch FORCE_FLAT orders during HALT (I-4 fix)."""
    adapter = _make_adapter(tmp_path)
    adapter.running = True

    mock_sg = MagicMock()
    mock_sg.state = StormGuardState.HALT
    mock_sg.is_halt_exempt.return_value = False
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
    # P1-4 follow-up: ``_api_worker`` now re-raises ``CancelledError`` after
    # cleanup so cooperative cancellation propagates upward.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)

    assert len(dispatched) == 1, "FORCE_FLAT orders must be dispatched even during HALT"
