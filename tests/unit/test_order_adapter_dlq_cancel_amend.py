"""Tests for M5: DLQ routing for CANCEL/AMEND target-not-found branches.

Covers:
- CANCEL target not found -> DLQ with VALIDATION_ERROR
- CANCEL target _PENDING_SENTINEL -> DLQ with VALIDATION_ERROR
- CANCEL target _TERMINAL_BEFORE_REGISTERED -> DLQ with VALIDATION_ERROR
- AMEND target not found -> DLQ with VALIDATION_ERROR
- AMEND target _PENDING_SENTINEL -> DLQ with VALIDATION_ERROR
- AMEND target _TERMINAL_BEFORE_REGISTERED -> DLQ with VALIDATION_ERROR
"""

from __future__ import annotations

import asyncio
from typing import Any
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
from hft_platform.order.adapter import (
    _PENDING_SENTINEL,
    _TERMINAL_BEFORE_REGISTERED,
    OrderAdapter,
)
from hft_platform.order.deadletter import RejectionReason


class _StubCodec:
    def encode_side(self, side: Any) -> str:
        return "Buy"

    def encode_tif(self, tif: Any) -> str:
        return "ROD"

    def encode_price_type(self, price_type: Any) -> str:
        return "LMT"


@pytest.fixture
def tmp_config(tmp_path):
    cfg = tmp_path / "order.yaml"
    cfg.write_text(
        "rate_limits:\n"
        "  shioaji_soft_cap: 180\n"
        "  shioaji_hard_cap: 250\n"
        "  window_seconds: 10\n"
        "circuit_breaker:\n"
        "  threshold: 5\n"
        "  timeout_seconds: 60\n"
    )
    return str(cfg)


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
        mp_inst.descale.return_value = 500.0
        mp.return_value = mp_inst
        meta_inst = MagicMock()
        meta_inst.order_params.return_value = {}
        ms.return_value = meta_inst
        yield


def _make_client():
    client = MagicMock()
    client.place_order = MagicMock(
        return_value=MagicMock(seq_no="S1", ord_no="O1", order_id="ID1", id="X1", order=None)
    )
    client.cancel_order = MagicMock(return_value=MagicMock())
    client.update_order = MagicMock(return_value=MagicMock())
    client.get_exchange = MagicMock(return_value="TSE")
    return client


def _make_intent(
    intent_type: IntentType = IntentType.CANCEL,
    *,
    intent_id: int = 1,
    strategy_id: str = "strat1",
    symbol: str = "2330",
    side: Side = Side.BUY,
    price: int = 500_0000,
    qty: int = 1,
    target_order_id: str | None = "99",
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
        target_order_id=target_order_id,
    )


def _make_cmd(intent: OrderIntent) -> OrderCommand:
    return OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=timebase.now_ns() + 10**10,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=0,
    )


def _make_adapter(tmp_config: str, client=None) -> OrderAdapter:
    if client is None:
        client = _make_client()
    q: asyncio.Queue = asyncio.Queue()
    return OrderAdapter(config_path=tmp_config, order_queue=q, broker_client=client, broker_codec=_StubCodec())


# ---------------------------------------------------------------------------
# CANCEL branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_target_not_found_routes_to_dlq(tmp_config):
    """CANCEL with missing target order routes to DLQ with CANCEL_TARGET_NOT_FOUND (Bug #37)."""
    adapter = _make_adapter(tmp_config)
    adapter._dlq = AsyncMock()
    intent = _make_intent(IntentType.CANCEL, target_order_id="nonexistent")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    adapter._dlq.add.assert_called_once()
    call_kwargs = adapter._dlq.add.call_args.kwargs
    assert call_kwargs["reason"] == RejectionReason.CANCEL_TARGET_NOT_FOUND
    assert "not found" in call_kwargs["error_message"].lower()


@pytest.mark.asyncio
async def test_cancel_pending_sentinel_routes_to_dlq(tmp_config):
    """CANCEL against a _PENDING_SENTINEL entry routes to DLQ with CANCEL_TARGET_PENDING (Bug #37)."""
    adapter = _make_adapter(tmp_config)
    adapter._dlq = AsyncMock()
    adapter.live_orders["strat1:99"] = _PENDING_SENTINEL
    intent = _make_intent(IntentType.CANCEL, target_order_id="99")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    adapter._dlq.add.assert_called_once()
    call_kwargs = adapter._dlq.add.call_args.kwargs
    assert call_kwargs["reason"] == RejectionReason.CANCEL_TARGET_PENDING
    assert "pending" in call_kwargs["error_message"].lower()


@pytest.mark.asyncio
async def test_cancel_terminal_before_registered_routes_to_dlq(tmp_config):
    """CANCEL against _TERMINAL_BEFORE_REGISTERED entry routes to DLQ with CANCEL_TARGET_TERMINAL (Bug #37)."""
    adapter = _make_adapter(tmp_config)
    adapter._dlq = AsyncMock()
    adapter.live_orders["strat1:99"] = _TERMINAL_BEFORE_REGISTERED
    intent = _make_intent(IntentType.CANCEL, target_order_id="99")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    adapter._dlq.add.assert_called_once()
    call_kwargs = adapter._dlq.add.call_args.kwargs
    assert call_kwargs["reason"] == RejectionReason.CANCEL_TARGET_TERMINAL
    assert "terminated" in call_kwargs["error_message"].lower()


# ---------------------------------------------------------------------------
# AMEND branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amend_target_not_found_routes_to_dlq(tmp_config):
    """AMEND with missing target order routes to DLQ with AMEND_TARGET_NOT_FOUND (Bug #37)."""
    adapter = _make_adapter(tmp_config)
    adapter._dlq = AsyncMock()
    intent = _make_intent(IntentType.AMEND, target_order_id="nonexistent")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    adapter._dlq.add.assert_called_once()
    call_kwargs = adapter._dlq.add.call_args.kwargs
    assert call_kwargs["reason"] == RejectionReason.AMEND_TARGET_NOT_FOUND
    assert "not found" in call_kwargs["error_message"].lower()


@pytest.mark.asyncio
async def test_amend_pending_sentinel_routes_to_dlq(tmp_config):
    """AMEND against a _PENDING_SENTINEL entry routes to DLQ with AMEND_TARGET_PENDING (Bug #37)."""
    adapter = _make_adapter(tmp_config)
    adapter._dlq = AsyncMock()
    adapter.live_orders["strat1:99"] = _PENDING_SENTINEL
    intent = _make_intent(IntentType.AMEND, target_order_id="99")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    adapter._dlq.add.assert_called_once()
    call_kwargs = adapter._dlq.add.call_args.kwargs
    assert call_kwargs["reason"] == RejectionReason.AMEND_TARGET_PENDING
    assert "pending" in call_kwargs["error_message"].lower()


@pytest.mark.asyncio
async def test_amend_terminal_before_registered_routes_to_dlq(tmp_config):
    """AMEND against _TERMINAL_BEFORE_REGISTERED entry routes to DLQ with AMEND_TARGET_TERMINAL (Bug #37)."""
    adapter = _make_adapter(tmp_config)
    adapter._dlq = AsyncMock()
    adapter.live_orders["strat1:99"] = _TERMINAL_BEFORE_REGISTERED
    intent = _make_intent(IntentType.AMEND, target_order_id="99")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    adapter._dlq.add.assert_called_once()
    call_kwargs = adapter._dlq.add.call_args.kwargs
    assert call_kwargs["reason"] == RejectionReason.AMEND_TARGET_TERMINAL
    assert "terminated" in call_kwargs["error_message"].lower()


# ---------------------------------------------------------------------------
# Metrics still incremented alongside DLQ
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_not_found_increments_reject_metric(tmp_config):
    """order_reject_total is incremented alongside DLQ routing."""
    adapter = _make_adapter(tmp_config)
    adapter._dlq = AsyncMock()
    intent = _make_intent(IntentType.CANCEL, target_order_id="missing")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    adapter.metrics.order_reject_total.inc.assert_called()
