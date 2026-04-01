"""Tests for OrderAdapter.execute() rejection cascade and dispatch logic."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.core import timebase
from hft_platform.core.rate_limiter import PerSymbolRateResult


def make_cmd(
    intent_type: IntentType = IntentType.NEW,
    symbol: str = "2330",
    price: int = 5_000_000,
    qty: int = 10,
    strategy_id: str = "s1",
) -> OrderCommand:
    intent = OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=Side.BUY,
        price=price,
        qty=qty,
    )
    now = timebase.now_ns()
    return OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=now + 10_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=now,
    )


@pytest.fixture
def tmp_config(tmp_path):
    cfg_file = tmp_path / "order_config.yaml"
    cfg_file.write_text(
        "rate_limits:\n"
        "  shioaji_soft_cap: 180\n"
        "  shioaji_hard_cap: 250\n"
        "  window_seconds: 10\n"
        "circuit_breaker:\n"
        "  threshold: 5\n"
        "  timeout_seconds: 60\n"
    )
    return str(cfg_file)


@pytest.fixture(autouse=True)
def mock_deps():
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder") as ml,
        patch("hft_platform.order.adapter.SymbolMetadata"),
        patch("hft_platform.order.adapter.PriceCodec"),
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as md,
    ):
        metrics_mock = MagicMock()
        metrics_mock.order_reject_total = MagicMock()
        metrics_mock.order_actions_total = MagicMock()
        metrics_mock.order_actions_total.labels.return_value = MagicMock()
        mm.get.return_value = metrics_mock
        ml.get.return_value = MagicMock()
        dlq_mock = AsyncMock()
        md.return_value = dlq_mock
        yield


@pytest.fixture
def adapter(tmp_config):
    from hft_platform.order.adapter import OrderAdapter

    q = asyncio.Queue()
    client = MagicMock()
    client.place_order = MagicMock(return_value=MagicMock())
    client.get_exchange = MagicMock(return_value="TSE")
    client.cancel_order = MagicMock(return_value=MagicMock())
    client.update_order = MagicMock(return_value=MagicMock())
    oa = OrderAdapter(
        config_path=tmp_config,
        order_queue=q,
        broker_client=client,
    )
    # Replace slots-based helpers with mocks so tests can configure them.
    # Defaults: all checks pass, shadow disabled, adapter not running.
    oa.per_symbol_rate_limiter = MagicMock()
    oa.per_symbol_rate_limiter.check.return_value = PerSymbolRateResult.OK
    oa.strategy_cb_mgr = MagicMock()
    oa.strategy_cb_mgr.is_open.return_value = False
    oa.circuit_breaker = MagicMock()
    oa.circuit_breaker.is_open.return_value = False
    oa.rate_limiter = MagicMock()
    oa.rate_limiter.check.return_value = True
    oa.shadow_sink = MagicMock()
    oa.shadow_sink.enabled = False
    return oa


# --- 1. Per-symbol rate limit HARD -> DLQ ---


@pytest.mark.asyncio
async def test_per_symbol_rate_limit_hard_rejects_to_dlq(adapter):
    """Per-symbol hard rate limit sends intent to DLQ with RATE_LIMIT."""
    adapter.per_symbol_rate_limiter.check.return_value = PerSymbolRateResult.HARD
    cmd = make_cmd()

    await adapter.execute(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert call_kwargs["reason"].value == "rate_limit"
    assert "Per-symbol" in call_kwargs["error_message"]


# --- 2. Per-strategy circuit breaker -> DLQ ---


@pytest.mark.asyncio
async def test_per_strategy_circuit_breaker_rejects_to_dlq(adapter):
    """Per-strategy CB open sends intent to DLQ with CIRCUIT_BREAKER."""
    adapter.strategy_cb_mgr.is_open.return_value = True
    cmd = make_cmd()

    await adapter.execute(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert call_kwargs["reason"].value == "circuit_breaker"
    assert "Per-strategy" in call_kwargs["error_message"]


# --- 3. Global circuit breaker -> DLQ ---


@pytest.mark.asyncio
async def test_global_circuit_breaker_rejects_to_dlq(adapter):
    """Global circuit breaker open sends intent to DLQ with CIRCUIT_BREAKER."""
    adapter.circuit_breaker.is_open.return_value = True
    cmd = make_cmd()

    await adapter.execute(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert call_kwargs["reason"].value == "circuit_breaker"
    assert "Circuit breaker open" in call_kwargs["error_message"]


# --- 4. Global rate limit -> DLQ ---


@pytest.mark.asyncio
async def test_global_rate_limit_rejects_to_dlq(adapter):
    """Global rate limit exceeded sends intent to DLQ with RATE_LIMIT."""
    adapter.rate_limiter.check.return_value = False
    cmd = make_cmd()

    await adapter.execute(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert call_kwargs["reason"].value == "rate_limit"


# --- 5. Shadow mode intercept (no DLQ) ---


@pytest.mark.asyncio
async def test_shadow_mode_intercepts_without_dlq(adapter):
    """Shadow mode intercepts the order without adding to DLQ."""
    adapter.shadow_sink.enabled = True
    cmd = make_cmd()

    await adapter.execute(cmd)

    adapter.shadow_sink.intercept.assert_called_once_with(cmd.intent)
    adapter._dlq.add.assert_not_awaited()


# --- 6. Client validation failure -> DLQ ---


@pytest.mark.asyncio
async def test_client_validation_failure_rejects_to_dlq(adapter):
    """Client missing required methods sends intent to DLQ with VALIDATION_ERROR."""
    # Remove place_order to fail validation for NEW intent
    del adapter.client.place_order
    cmd = make_cmd(intent_type=IntentType.NEW)

    await adapter.execute(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert call_kwargs["reason"].value == "validation_error"


# --- 7. Expired deadline skipped in run loop ---


@pytest.mark.asyncio
async def test_expired_deadline_skipped_in_run(adapter):
    """Commands past deadline are skipped in the run loop."""
    cmd = make_cmd()
    import time

    cmd.deadline_ns = time.monotonic_ns() - 1_000_000

    adapter.order_queue.put_nowait(cmd)

    execute_called = False

    async def track_execute(*_args, **_kwargs):
        nonlocal execute_called
        execute_called = True
        adapter.running = False

    adapter.execute = track_execute

    async def run_with_timeout():
        adapter.running = True
        try:
            await asyncio.wait_for(adapter.run(), timeout=1.0)
        except asyncio.TimeoutError:
            adapter.running = False

    await run_with_timeout()

    # execute should NOT have been called since deadline expired
    assert not execute_called


# --- 8. NEW order dispatched successfully (not running) ---


@pytest.mark.asyncio
async def test_new_order_dispatch_not_running(adapter):
    """NEW order dispatched directly when adapter is not running."""
    adapter.running = False
    adapter._dispatch_to_api = AsyncMock()
    cmd = make_cmd(intent_type=IntentType.NEW)

    await adapter.execute(cmd)

    adapter._dispatch_to_api.assert_awaited_once_with(cmd)
    adapter._dlq.add.assert_not_awaited()
    assert adapter._dispatch_to_api.await_count == 1


# --- 9. CANCEL order dispatched (not running) ---


@pytest.mark.asyncio
async def test_cancel_order_dispatch_not_running(adapter):
    """CANCEL order dispatched directly when adapter is not running."""
    adapter.running = False
    adapter._dispatch_to_api = AsyncMock()
    cmd = make_cmd(intent_type=IntentType.CANCEL)

    await adapter.execute(cmd)

    adapter._dispatch_to_api.assert_awaited_once_with(cmd)
    assert adapter._dispatch_to_api.await_count == 1


# --- 10. AMEND order dispatched (not running) ---


@pytest.mark.asyncio
async def test_amend_order_dispatch_not_running(adapter):
    """AMEND order dispatched directly when adapter is not running."""
    adapter.running = False
    adapter._dispatch_to_api = AsyncMock()
    cmd = make_cmd(intent_type=IntentType.AMEND)

    await adapter.execute(cmd)

    adapter._dispatch_to_api.assert_awaited_once_with(cmd)
    assert adapter._dispatch_to_api.await_count == 1


# --- 11. Per-symbol rate limit HARD DLQ fields ---


@pytest.mark.asyncio
async def test_per_symbol_hard_rate_limit_dlq_fields(adapter):
    """Per-symbol HARD rate limit DLQ entry has correct symbol and strategy."""
    adapter.per_symbol_rate_limiter.check.return_value = PerSymbolRateResult.HARD
    cmd = make_cmd(symbol="2454", strategy_id="mm1")

    await adapter.execute(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert call_kwargs["symbol"] == "2454"
    assert call_kwargs["strategy_id"] == "mm1"
    assert call_kwargs["reason"].value == "rate_limit"


# --- 12. Multiple rejections in sequence ---


@pytest.mark.asyncio
async def test_multiple_rejections_in_sequence(adapter):
    """Multiple rejected commands each produce separate DLQ entries."""
    adapter.per_symbol_rate_limiter.check.return_value = PerSymbolRateResult.HARD

    cmd1 = make_cmd(symbol="2330")
    cmd2 = make_cmd(symbol="2454")
    cmd3 = make_cmd(symbol="3008")

    await adapter.execute(cmd1)
    await adapter.execute(cmd2)
    await adapter.execute(cmd3)

    assert adapter._dlq.add.await_count == 3


# --- 13. DLQ error handling (DLQ.add raises) ---


@pytest.mark.asyncio
async def test_dlq_add_error_propagates(adapter):
    """If DLQ.add raises, execute() propagates the exception."""
    adapter.per_symbol_rate_limiter.check.return_value = PerSymbolRateResult.HARD
    adapter._dlq.add = AsyncMock(side_effect=RuntimeError("DLQ write failed"))
    cmd = make_cmd()

    with pytest.raises(RuntimeError, match="DLQ write failed"):
        await adapter.execute(cmd)


# --- 14. _validate_client for NEW (needs place_order + get_exchange) ---


@pytest.mark.asyncio
async def test_validate_client_new_requires_place_order_and_get_exchange(adapter):
    """NEW intent requires client to have place_order and get_exchange."""
    intent_new = make_cmd(intent_type=IntentType.NEW).intent

    # With both methods present - should pass
    assert adapter._validate_client(intent_new) is True

    # Remove get_exchange - should fail
    del adapter.client.get_exchange
    assert adapter._validate_client(intent_new) is False


# --- 15. _validate_client for CANCEL (needs cancel_order) ---


@pytest.mark.asyncio
async def test_validate_client_cancel_requires_cancel_order(adapter):
    """CANCEL intent requires client to have cancel_order."""
    intent_cancel = make_cmd(intent_type=IntentType.CANCEL).intent

    # With cancel_order present - should pass
    assert adapter._validate_client(intent_cancel) is True

    # Remove cancel_order - should fail
    del adapter.client.cancel_order
    assert adapter._validate_client(intent_cancel) is False


# --- 16. Running adapter enqueues to API queue ---


@pytest.mark.asyncio
async def test_running_adapter_enqueues_to_api_queue(adapter):
    """When adapter is running, execute() enqueues to _api_queue."""
    adapter.running = True
    cmd = make_cmd(intent_type=IntentType.NEW)

    await adapter.execute(cmd)

    assert adapter._api_queue.qsize() == 1
    queued = adapter._api_queue.get_nowait()
    assert queued is cmd


# --- 17. StormGuard HALT blocks order ---


@pytest.mark.asyncio
async def test_storm_guard_halt_rejects_to_dlq(adapter):
    """Order with storm_guard_state=HALT is DLQ'd before any other check."""
    cmd = make_cmd()
    cmd.storm_guard_state = StormGuardState.HALT

    await adapter.execute(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert call_kwargs["reason"].value == "validation_error"
    assert "StormGuard HALT" in call_kwargs["error_message"]
    # Confirm no other checks ran (per-symbol rate limiter never called)
    adapter.per_symbol_rate_limiter.check.assert_not_called()


# --- 18. StormGuard HALT allows halt_flatten orders ---


@pytest.mark.asyncio
async def test_storm_guard_halt_allows_halt_flatten_order(adapter):
    """halt_flatten orders bypass the StormGuard HALT check and proceed normally."""
    from hft_platform.contracts.strategy import OrderIntent

    intent = OrderIntent(
        intent_id=2,
        strategy_id="s1",
        symbol="2330",
        intent_type=IntentType.NEW,
        side=Side.SELL,
        price=5_000_000,
        qty=10,
        reason="halt_flatten",
    )
    now = timebase.now_ns()
    cmd = OrderCommand(
        cmd_id=2,
        intent=intent,
        deadline_ns=now + 10_000_000_000,
        storm_guard_state=StormGuardState.HALT,
        created_ns=now,
    )
    adapter.running = False
    adapter._dispatch_to_api = AsyncMock()

    await adapter.execute(cmd)

    # Should NOT be DLQ'd — should be dispatched
    adapter._dlq.add.assert_not_awaited()
    adapter._dispatch_to_api.assert_awaited_once_with(cmd)


# --- 19. StormGuard HALT allows CANCEL orders ---


@pytest.mark.asyncio
async def test_storm_guard_halt_allows_cancel_order(adapter):
    """CANCEL orders bypass StormGuard HALT (Constitution: HALT blocks new, allows cancels)."""
    cmd = make_cmd(intent_type=IntentType.CANCEL)
    cmd.intent.target_order_id = "s1:1"
    cmd.storm_guard_state = StormGuardState.HALT
    adapter.running = False
    adapter._dispatch_to_api = AsyncMock()

    await adapter.execute(cmd)

    adapter._dlq.add.assert_not_awaited()
    adapter._dispatch_to_api.assert_awaited_once_with(cmd)


# --- 20. StormGuard HALT allows FORCE_FLAT orders ---


@pytest.mark.asyncio
async def test_storm_guard_halt_allows_force_flat_order(adapter):
    """FORCE_FLAT orders bypass StormGuard HALT (Constitution: safety orders always allowed)."""
    cmd = make_cmd(intent_type=IntentType.FORCE_FLAT)
    cmd.storm_guard_state = StormGuardState.HALT
    adapter.running = False
    adapter._dispatch_to_api = AsyncMock()

    await adapter.execute(cmd)

    adapter._dlq.add.assert_not_awaited()
    adapter._dispatch_to_api.assert_awaited_once_with(cmd)


# --- 21. StormGuard HALT still blocks NEW orders ---


@pytest.mark.asyncio
async def test_storm_guard_halt_blocks_new_order(adapter):
    """NEW orders are still blocked during HALT (not exempt)."""
    cmd = make_cmd(intent_type=IntentType.NEW)
    cmd.storm_guard_state = StormGuardState.HALT

    await adapter.execute(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert "StormGuard HALT" in call_kwargs["error_message"]


# --- 22. StormGuard HALT still blocks AMEND orders ---


@pytest.mark.asyncio
async def test_storm_guard_halt_blocks_amend_order(adapter):
    """AMEND orders are blocked during HALT (not safety-critical)."""
    cmd = make_cmd(intent_type=IntentType.AMEND)
    cmd.intent.target_order_id = "s1:1"
    cmd.storm_guard_state = StormGuardState.HALT

    await adapter.execute(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert "StormGuard HALT" in call_kwargs["error_message"]


# --- 23. API queue full routes to DLQ + metric ---


@pytest.mark.asyncio
async def test_api_queue_full_routes_to_dlq(adapter):
    """When API queue is full, order is routed to DLQ with reject metric."""
    cmd = make_cmd()
    # Make the API queue full
    adapter._api_queue = asyncio.Queue(maxsize=1)
    adapter._api_queue.put_nowait(make_cmd())  # fill it

    adapter.running = False
    await adapter._enqueue_api(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert "API queue full" in call_kwargs["error_message"]
    adapter.metrics.order_reject_total.inc.assert_called()
