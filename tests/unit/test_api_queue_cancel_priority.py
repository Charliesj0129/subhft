"""H7: CANCEL should evict a NEW intent when api_queue is full.

Root cause: ``_enqueue_api`` applies the same DLQ fate to both CANCEL
and NEW on ``asyncio.QueueFull``. Under sustained queue pressure, a
CANCEL dropped while a matching NEW survives leaves a resting order
at the broker with no way to cancel it locally — orphan state with
real position risk.

Fix: on QueueFull, a CANCEL preempts the oldest NEW in the queue (the
preempted NEW is routed to DLQ). If no NEW is available to evict, the
CANCEL itself falls through to DLQ (and surfaces a dedicated metric).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, StormGuardState
from hft_platform.order.adapter import OrderAdapter, OrderCommand


@pytest.fixture
def tmp_config(tmp_path: Path) -> str:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("rate_limits: {}\ncircuit_breaker: {}\n")
    return str(cfg)


def _make_adapter(tmp_config: str, queue_size: int = 2) -> OrderAdapter:
    client = MagicMock()
    client.place_order = MagicMock(return_value=MagicMock())
    client.cancel_order = MagicMock(return_value=MagicMock())
    client.mode = "simulation"
    client.activate_ca = False
    q: asyncio.Queue = asyncio.Queue(maxsize=16)
    adapter = OrderAdapter(
        config_path=tmp_config,
        order_queue=q,
        broker_client=client,
    )
    adapter.shadow_sink.enabled = False
    # Force the api_queue to a tiny capacity for the test.
    adapter._api_queue = asyncio.Queue(maxsize=queue_size)
    return adapter


def _mk_cmd(intent_type: IntentType, intent_id: int, target_order_id: str = "") -> OrderCommand:
    intent = OrderIntent(
        intent_id=intent_id,
        strategy_id="S1",
        symbol="TMFD6",
        intent_type=intent_type,
        side=Side.BUY,
        price=10000 if intent_type == IntentType.NEW else 0,
        qty=1 if intent_type == IntentType.NEW else 0,
        target_order_id=target_order_id,
    )
    return OrderCommand(
        cmd_id=intent_id,
        intent=intent,
        deadline_ns=0,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=0,
    )


@pytest.mark.asyncio
async def test_cancel_preempts_new_on_queue_full(tmp_config: str):
    adapter = _make_adapter(tmp_config, queue_size=2)
    new1 = _mk_cmd(IntentType.NEW, 1)
    new2 = _mk_cmd(IntentType.NEW, 2)
    cancel = _mk_cmd(IntentType.CANCEL, 99, target_order_id="broker_id_xyz")

    # Fill the queue with NEWs
    assert await adapter._enqueue_api(new1) is True
    assert await adapter._enqueue_api(new2) is True
    assert adapter._api_queue.qsize() == 2

    # CANCEL should preempt a NEW (not fall through to DLQ).
    ok = await adapter._enqueue_api(cancel)
    assert ok is True, "CANCEL must not be dropped when a NEW is evictable"

    remaining = []
    while not adapter._api_queue.empty():
        remaining.append(adapter._api_queue.get_nowait())
    remaining_types = [cmd.intent.intent_type for cmd in remaining]
    assert IntentType.CANCEL in remaining_types
    assert remaining_types.count(IntentType.NEW) == 1  # exactly one NEW evicted


@pytest.mark.asyncio
async def test_cancel_dlqs_if_no_new_to_evict(tmp_config: str):
    adapter = _make_adapter(tmp_config, queue_size=1)
    cancel_existing = _mk_cmd(IntentType.CANCEL, 50, target_order_id="old")
    cancel_new = _mk_cmd(IntentType.CANCEL, 51, target_order_id="new")

    assert await adapter._enqueue_api(cancel_existing) is True
    # No NEW present to evict — CANCEL falls through to DLQ.
    ok = await adapter._enqueue_api(cancel_new)
    assert ok is False
