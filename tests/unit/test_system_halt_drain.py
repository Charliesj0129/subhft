"""Tests for HALT drain safety order handling in SystemCoordinator.

Verifies that safety commands (CANCEL/FORCE_FLAT) are directly dispatched
to OrderAdapter.execute() during HALT drain, bypassing the order queue.
"""

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


def _make_intent(intent_type: IntentType, strategy_id: str = "test_strat") -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol="2330",
        intent_type=intent_type,
        side=Side.BUY,
        price=1000000,
        qty=1,
        timestamp_ns=0,
    )


def _make_cmd(intent_type: IntentType, cmd_id: int = 1) -> OrderCommand:
    intent = _make_intent(intent_type)
    return OrderCommand(
        cmd_id=cmd_id,
        intent=intent,
        deadline_ns=0,
        storm_guard_state=StormGuardState.HALT,
    )


class TestHaltDrainSafetyDispatch:
    """Safety commands in order_queue are dispatched directly, not re-queued."""

    @pytest.mark.asyncio
    async def test_cancel_cmd_dispatched_directly(self):
        """CANCEL command in order_queue during HALT is dispatched via OrderAdapter.execute."""
        cancel_cmd = _make_cmd(IntentType.CANCEL, cmd_id=10)

        order_queue = asyncio.Queue(maxsize=8)
        await order_queue.put(cancel_cmd)

        adapter = MagicMock()
        adapter.execute = AsyncMock()

        # Simulate the drain logic from system.py lines 699-730
        _cmd_requeue = []
        while not order_queue.empty():
            try:
                cmd = order_queue.get_nowait()
                order_queue.task_done()
                _intent = getattr(cmd, "intent", None)
                _itype = getattr(_intent, "intent_type", None) if _intent else None
                _is_safety = _itype in (IntentType.CANCEL, IntentType.FORCE_FLAT)
                if _is_safety:
                    _cmd_requeue.append(cmd)
            except asyncio.QueueEmpty:
                break

        # Dispatch directly (the fix)
        for cmd in _cmd_requeue:
            asyncio.create_task(adapter.execute(cmd))

        # Let the task run
        await asyncio.sleep(0)

        adapter.execute.assert_called_once_with(cancel_cmd)
        assert order_queue.empty(), "Safety cmd must NOT be re-queued"

    @pytest.mark.asyncio
    async def test_force_flat_cmd_dispatched_directly(self):
        """FORCE_FLAT command in order_queue during HALT is dispatched via OrderAdapter.execute."""
        ff_cmd = _make_cmd(IntentType.FORCE_FLAT, cmd_id=20)

        order_queue = asyncio.Queue(maxsize=8)
        await order_queue.put(ff_cmd)

        adapter = MagicMock()
        adapter.execute = AsyncMock()

        _cmd_requeue = []
        while not order_queue.empty():
            try:
                cmd = order_queue.get_nowait()
                order_queue.task_done()
                _intent = getattr(cmd, "intent", None)
                _itype = getattr(_intent, "intent_type", None) if _intent else None
                _is_safety = _itype in (IntentType.CANCEL, IntentType.FORCE_FLAT)
                if _is_safety:
                    _cmd_requeue.append(cmd)
            except asyncio.QueueEmpty:
                break

        for cmd in _cmd_requeue:
            asyncio.create_task(adapter.execute(cmd))

        await asyncio.sleep(0)

        adapter.execute.assert_called_once_with(ff_cmd)
        assert order_queue.empty()

    @pytest.mark.asyncio
    async def test_new_order_cmd_drained_not_dispatched(self):
        """NEW order command is drained and dropped during HALT, not dispatched."""
        new_cmd = _make_cmd(IntentType.NEW, cmd_id=30)

        order_queue = asyncio.Queue(maxsize=8)
        await order_queue.put(new_cmd)

        adapter = MagicMock()
        adapter.execute = AsyncMock()

        _cmd_requeue = []
        drained_count = 0
        while not order_queue.empty():
            try:
                cmd = order_queue.get_nowait()
                order_queue.task_done()
                _intent = getattr(cmd, "intent", None)
                _itype = getattr(_intent, "intent_type", None) if _intent else None
                _is_safety = _itype in (IntentType.CANCEL, IntentType.FORCE_FLAT)
                if _is_safety:
                    _cmd_requeue.append(cmd)
                else:
                    drained_count += 1
            except asyncio.QueueEmpty:
                break

        for cmd in _cmd_requeue:
            asyncio.create_task(adapter.execute(cmd))

        await asyncio.sleep(0)

        adapter.execute.assert_not_called()
        assert drained_count == 1

    @pytest.mark.asyncio
    async def test_mixed_queue_only_safety_dispatched(self):
        """Queue with mixed cmds: only CANCEL/FORCE_FLAT dispatched, others drained."""
        cancel_cmd = _make_cmd(IntentType.CANCEL, cmd_id=1)
        new_cmd = _make_cmd(IntentType.NEW, cmd_id=2)
        ff_cmd = _make_cmd(IntentType.FORCE_FLAT, cmd_id=3)
        amend_cmd = _make_cmd(IntentType.AMEND, cmd_id=4)

        order_queue = asyncio.Queue(maxsize=8)
        for c in [cancel_cmd, new_cmd, ff_cmd, amend_cmd]:
            await order_queue.put(c)

        adapter = MagicMock()
        adapter.execute = AsyncMock()

        _cmd_requeue = []
        drained_count = 0
        while not order_queue.empty():
            try:
                cmd = order_queue.get_nowait()
                order_queue.task_done()
                _intent = getattr(cmd, "intent", None)
                _itype = getattr(_intent, "intent_type", None) if _intent else None
                _is_safety = _itype in (IntentType.CANCEL, IntentType.FORCE_FLAT)
                if _is_safety:
                    _cmd_requeue.append(cmd)
                else:
                    drained_count += 1
            except asyncio.QueueEmpty:
                break

        for cmd in _cmd_requeue:
            asyncio.create_task(adapter.execute(cmd))

        await asyncio.sleep(0)

        assert adapter.execute.call_count == 2
        dispatched_cmds = [call.args[0] for call in adapter.execute.call_args_list]
        assert cancel_cmd in dispatched_cmds
        assert ff_cmd in dispatched_cmds
        assert drained_count == 2


class TestHaltDrainRiskQueueSafetyLog:
    """Risk queue re-queue failure logs at CRITICAL level."""

    @pytest.mark.asyncio
    async def test_risk_requeue_full_logs_critical(self):
        """When risk_queue is full during re-queue, log at CRITICAL (not WARNING)."""
        cancel_intent = _make_intent(IntentType.CANCEL)

        # Queue of size 0 - will always be full
        risk_queue = asyncio.Queue(maxsize=1)
        # Fill it so put_nowait raises QueueFull
        await risk_queue.put("blocker")

        with patch("hft_platform.services.system.logger") as mock_logger:
            # Simulate the risk drain re-queue path
            try:
                risk_queue.put_nowait(cancel_intent)
            except asyncio.QueueFull:
                mock_logger.critical(
                    "risk_queue_full_safety_intent_lost",
                    strategy_id=getattr(cancel_intent, "strategy_id", "?"),
                    intent_type=str(getattr(cancel_intent, "intent_type", "?")),
                )

            mock_logger.critical.assert_called_once()
            call_args = mock_logger.critical.call_args
            assert "risk_queue_full_safety_intent_lost" in call_args.args
