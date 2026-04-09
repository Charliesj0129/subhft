"""Tests for per-strategy circuit breaker wiring in OrderAdapter.

Verifies that record_failure / record_success are called on
strategy_cb_mgr alongside the global circuit_breaker, and that
enough failures cause is_open() to return True and block orders.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
from hft_platform.order.circuit_breaker import StrategyCircuitBreakerManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STRATEGY_ID = "strat_test_cb"


def _make_intent(**overrides: Any) -> OrderIntent:
    defaults: dict[str, Any] = {
        "intent_id": 1,
        "strategy_id": STRATEGY_ID,
        "symbol": "2330",
        "intent_type": IntentType.NEW,
        "side": Side.BUY,
        "price": 5950000,
        "qty": 1,
        "tif": TIF.LIMIT,
    }
    defaults.update(overrides)
    return OrderIntent(**defaults)


def _make_cmd(intent: OrderIntent, cmd_id: int = 1) -> OrderCommand:
    deadline_ns = time.monotonic_ns() + 10_000_000_000  # 10s
    return OrderCommand(
        cmd_id=cmd_id,
        intent=intent,
        deadline_ns=deadline_ns,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=timebase.now_ns(),
    )


def _make_adapter(tmp_path: Any, client: Any = None) -> OrderAdapter:
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
        client.place_order = MagicMock(return_value=MagicMock(seq_no="S1", ord_no="O1", id="X1", order=None))
        client.cancel_order = MagicMock()
        client.get_exchange = MagicMock(return_value="TSE")
    queue: asyncio.Queue[OrderCommand] = asyncio.Queue()
    adapter = OrderAdapter(str(config_file), queue, client)
    return adapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_deps():
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder") as ml,
        patch("hft_platform.order.adapter.SymbolMetadata") as ms,
        patch("hft_platform.order.adapter.PriceCodec") as mp,
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as md,
    ):
        mm.get.return_value = MagicMock()
        ml.get.return_value = MagicMock()
        md.return_value = AsyncMock()
        mp_inst = MagicMock()
        mp_inst.descale.return_value = 595.0
        mp.return_value = mp_inst
        meta_inst = MagicMock()
        meta_inst.order_params.return_value = {}
        ms.return_value = meta_inst
        yield


# ---------------------------------------------------------------------------
# Test 1: broker error on NEW order → record_failure called on strategy CB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broker_error_calls_strategy_cb_record_failure(tmp_path):
    """After a broker OSError on a NEW order, strategy_cb_mgr.record_failure is called."""
    client = MagicMock()
    client.place_order = MagicMock(side_effect=OSError("broker connection lost"))
    client.get_exchange = MagicMock(return_value="TSE")

    adapter = _make_adapter(tmp_path, client=client)

    # Patch strategy_cb_mgr so we can track calls
    mock_scb = MagicMock(spec=StrategyCircuitBreakerManager)
    mock_scb.is_open.return_value = False
    adapter.strategy_cb_mgr = mock_scb

    # Set up codec so dispatch proceeds past validation
    codec = MagicMock()
    codec.encode_side.return_value = "Buy"
    codec.encode_tif.return_value = "ROD"
    codec.encode_price_type.return_value = "LMT"
    adapter._broker_codec = codec

    intent = _make_intent()
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    mock_scb.record_failure.assert_called_once_with(STRATEGY_ID)
    mock_scb.record_success.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: successful NEW order → record_success called on strategy CB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_new_order_calls_strategy_cb_record_success(tmp_path):
    """After a successful NEW order place, strategy_cb_mgr.record_success is called."""
    trade_mock = MagicMock()
    trade_mock.seq_no = "S1"
    trade_mock.ord_no = "O1"
    trade_mock.id = "X1"
    trade_mock.order = None

    client = MagicMock()
    client.place_order = MagicMock(return_value=trade_mock)
    client.get_exchange = MagicMock(return_value="TSE")

    adapter = _make_adapter(tmp_path, client=client)

    mock_scb = MagicMock(spec=StrategyCircuitBreakerManager)
    mock_scb.is_open.return_value = False
    adapter.strategy_cb_mgr = mock_scb

    codec = MagicMock()
    codec.encode_side.return_value = "Buy"
    codec.encode_tif.return_value = "ROD"
    codec.encode_price_type.return_value = "LMT"
    adapter._broker_codec = codec

    intent = _make_intent()
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    # record_success may be called more than once (once in _call_api, once in _dispatch_to_api)
    assert mock_scb.record_success.call_count >= 1
    assert all(call.args == (STRATEGY_ID,) for call in mock_scb.record_success.call_args_list)
    mock_scb.record_failure.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: after enough failures, is_open returns True and order is rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategy_cb_open_blocks_new_order(tmp_path):
    """Once strategy_cb_mgr.is_open returns True, the order is rejected via DLQ."""
    client = MagicMock()
    client.place_order = MagicMock(return_value=MagicMock(seq_no="S1", ord_no="O1", id="X1", order=None))
    client.get_exchange = MagicMock(return_value="TSE")

    adapter = _make_adapter(tmp_path, client=client)

    # Configure a real StrategyCircuitBreakerManager with threshold=2
    real_scb = StrategyCircuitBreakerManager(default_threshold=2, default_timeout_s=60)
    adapter.strategy_cb_mgr = real_scb

    # Simulate 2 failures to trip the breaker
    real_scb.record_failure(STRATEGY_ID)
    real_scb.record_failure(STRATEGY_ID)

    # Breaker should now be open
    assert real_scb.is_open(STRATEGY_ID), "Breaker must be open after threshold failures"

    dlq_received: list[tuple] = []
    original_add_to_dlq = adapter._add_to_dlq

    async def _capture_dlq(intent, reason, msg=""):
        dlq_received.append((intent, reason, msg))

    adapter._add_to_dlq = _capture_dlq  # type: ignore[method-assign]

    # Validate client so we get past validation
    codec = MagicMock()
    codec.encode_side.return_value = "Buy"
    codec.encode_tif.return_value = "ROD"
    codec.encode_price_type.return_value = "LMT"
    adapter._broker_codec = codec

    intent = _make_intent(intent_id=99)
    cmd = _make_cmd(intent, cmd_id=99)

    # Use execute which is the entry point that checks strategy CB
    adapter._validate_client = MagicMock(return_value=True)  # type: ignore[method-assign]
    await adapter.execute(cmd)

    # The order must have been DLQ'd, not placed
    assert len(dlq_received) == 1, f"Expected 1 DLQ entry, got {dlq_received}"
    _, reason, msg = dlq_received[0]
    assert "circuit" in msg.lower(), f"Unexpected DLQ message: {msg!r}"
    client.place_order.assert_not_called()
