"""M3: ``_api_queue`` priority-based eviction.

Generalises the H7 "CANCEL preempts NEW" rule to a full priority order:
``CANCEL > FORCE_FLAT > AMEND > NEW``. When the queue is full, an
incoming high-priority intent evicts the lowest-priority (oldest in
ties) queued item — preventing AMEND/CANCEL/FORCE_FLAT starvation
under sustained NEW pressure.
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
    client.update_order = MagicMock(return_value=MagicMock())
    client.mode = "simulation"
    client.activate_ca = False
    q: asyncio.Queue = asyncio.Queue(maxsize=16)
    adapter = OrderAdapter(
        config_path=tmp_config,
        order_queue=q,
        broker_client=client,
    )
    adapter.shadow_sink.enabled = False
    adapter._api_queue = asyncio.Queue(maxsize=queue_size)
    return adapter


def _mk_cmd(
    intent_type: IntentType,
    intent_id: int,
    target_order_id: str = "",
    strategy_id: str = "S1",
) -> OrderCommand:
    intent = OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
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


class TestPriorityOrdering:
    def test_priority_table_matches_spec(self):
        # CANCEL > FORCE_FLAT > AMEND > NEW
        pri = OrderAdapter._api_intent_priority
        assert pri(IntentType.CANCEL) > pri(IntentType.FORCE_FLAT)
        assert pri(IntentType.FORCE_FLAT) > pri(IntentType.AMEND)
        assert pri(IntentType.AMEND) > pri(IntentType.NEW)
        assert pri(None) == 0
        assert pri(IntentType.NEW) > 0


class TestEvictionPolicy:
    @pytest.mark.asyncio
    async def test_amend_evicts_new(self, tmp_config: str):
        """AMEND priority > NEW priority, so AMEND must evict NEW when queue full."""
        adapter = _make_adapter(tmp_config, queue_size=2)
        n1 = _mk_cmd(IntentType.NEW, 1)
        n2 = _mk_cmd(IntentType.NEW, 2)
        amend = _mk_cmd(IntentType.AMEND, 99, target_order_id="bid_target")

        assert await adapter._enqueue_api(n1) is True
        assert await adapter._enqueue_api(n2) is True
        assert adapter._api_queue.qsize() == 2

        ok = await adapter._enqueue_api(amend)
        assert ok is True, "AMEND must evict NEW when queue is full"

        remaining = []
        while not adapter._api_queue.empty():
            remaining.append(adapter._api_queue.get_nowait())
        types = [c.intent.intent_type for c in remaining]
        assert IntentType.AMEND in types
        assert types.count(IntentType.NEW) == 1  # exactly one NEW evicted

    @pytest.mark.asyncio
    async def test_cancel_evicts_amend_when_no_new(self, tmp_config: str):
        """When the queue is full of AMENDs, CANCEL preempts the oldest AMEND."""
        adapter = _make_adapter(tmp_config, queue_size=2)
        a1 = _mk_cmd(IntentType.AMEND, 1, target_order_id="t1")
        a2 = _mk_cmd(IntentType.AMEND, 2, target_order_id="t2")
        cancel = _mk_cmd(IntentType.CANCEL, 99, target_order_id="t3")

        assert await adapter._enqueue_api(a1) is True
        assert await adapter._enqueue_api(a2) is True
        ok = await adapter._enqueue_api(cancel)
        assert ok is True

        remaining = [
            adapter._api_queue.get_nowait()
            for _ in range(adapter._api_queue.qsize())
        ]
        types = [c.intent.intent_type for c in remaining]
        assert IntentType.CANCEL in types
        assert types.count(IntentType.AMEND) == 1

    @pytest.mark.asyncio
    async def test_cancel_prefers_new_over_amend(self, tmp_config: str):
        """When both NEW and AMEND queued, CANCEL evicts NEW (lower priority)."""
        adapter = _make_adapter(tmp_config, queue_size=2)
        n = _mk_cmd(IntentType.NEW, 1)
        a = _mk_cmd(IntentType.AMEND, 2, target_order_id="t")
        cancel = _mk_cmd(IntentType.CANCEL, 99, target_order_id="x")

        await adapter._enqueue_api(n)
        await adapter._enqueue_api(a)
        ok = await adapter._enqueue_api(cancel)
        assert ok is True

        remaining = [
            adapter._api_queue.get_nowait()
            for _ in range(adapter._api_queue.qsize())
        ]
        types = [c.intent.intent_type for c in remaining]
        assert IntentType.CANCEL in types
        assert IntentType.AMEND in types  # AMEND survives (higher priority than NEW)
        assert IntentType.NEW not in types

    @pytest.mark.asyncio
    async def test_new_cannot_evict(self, tmp_config: str):
        """NEW never preempts; queue full + NEW arriving → DLQ."""
        adapter = _make_adapter(tmp_config, queue_size=1)
        first = _mk_cmd(IntentType.NEW, 1)
        second = _mk_cmd(IntentType.NEW, 2)

        assert await adapter._enqueue_api(first) is True
        ok = await adapter._enqueue_api(second)
        assert ok is False, "NEW must not preempt anything; second NEW DLQ'd"

    @pytest.mark.asyncio
    async def test_amend_cannot_evict_force_flat(self, tmp_config: str):
        """FORCE_FLAT priority > AMEND priority, so AMEND cannot evict FORCE_FLAT."""
        adapter = _make_adapter(tmp_config, queue_size=1)
        ff = _mk_cmd(IntentType.FORCE_FLAT, 1)
        amend = _mk_cmd(IntentType.AMEND, 2, target_order_id="t")

        assert await adapter._enqueue_api(ff) is True
        ok = await adapter._enqueue_api(amend)
        assert ok is False, "AMEND cannot evict FORCE_FLAT"

    @pytest.mark.asyncio
    async def test_force_flat_evicts_new(self, tmp_config: str):
        adapter = _make_adapter(tmp_config, queue_size=1)
        n = _mk_cmd(IntentType.NEW, 1)
        ff = _mk_cmd(IntentType.FORCE_FLAT, 2)

        assert await adapter._enqueue_api(n) is True
        ok = await adapter._enqueue_api(ff)
        assert ok is True

        remaining = [
            adapter._api_queue.get_nowait()
            for _ in range(adapter._api_queue.qsize())
        ]
        assert [c.intent.intent_type for c in remaining] == [IntentType.FORCE_FLAT]


class TestHelperBackwardsCompat:
    @pytest.mark.asyncio
    async def test_evict_new_for_cancel_alias(self, tmp_config: str):
        """The legacy ``_evict_new_for_cancel`` name is preserved as an
        alias to keep any external test fixtures working."""
        adapter = _make_adapter(tmp_config, queue_size=2)
        n = _mk_cmd(IntentType.NEW, 1)
        a = _mk_cmd(IntentType.AMEND, 2, target_order_id="t")
        await adapter._enqueue_api(n)
        await adapter._enqueue_api(a)
        evicted = adapter._evict_new_for_cancel()
        assert evicted is not None
        assert evicted.intent.intent_type == IntentType.NEW

    @pytest.mark.asyncio
    async def test_evict_returns_none_on_empty_queue(self, tmp_config: str):
        adapter = _make_adapter(tmp_config, queue_size=2)
        assert (
            adapter._evict_lower_priority_for_safety_intent(IntentType.CANCEL)
            is None
        )
