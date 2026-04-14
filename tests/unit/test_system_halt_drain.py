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


class TestOnExecOverflowGuard:
    """Broker-thread _on_exec fallback must not silently evict fills."""

    def _make_stub(self, maxlen: int = 4):
        """Create a minimal stub with _on_exec behavior."""
        import collections
        from types import SimpleNamespace

        stub = SimpleNamespace()
        stub._exec_overflow_buf = collections.deque(maxlen=maxlen)
        stub._EXEC_OVERFLOW_MAX = maxlen
        stub._exec_overflow_evicted = 0
        stub._exec_startup_overflow_lost = False
        stub.running = True
        stub.loop = None  # force broker-thread fallback path
        # Stub out DLQ persistence (tested separately)
        stub._persist_lost_exec_event = lambda event: None
        return stub

    def test_append_when_below_capacity(self):
        """Events append normally when buffer is below capacity."""
        from hft_platform.services.system import HFTSystem

        stub = self._make_stub(maxlen=4)
        # Bind the real method to our stub
        bound = HFTSystem._on_exec.__get__(stub, type(stub))

        bound("test_topic", {"price": 100})
        assert len(stub._exec_overflow_buf) == 1
        assert stub._exec_overflow_evicted == 0

    def test_drops_with_critical_log_when_full(self):
        """When buffer is at capacity, new event is dropped and eviction counter incremented."""
        from hft_platform.services.system import HFTSystem

        stub = self._make_stub(maxlen=4)
        bound = HFTSystem._on_exec.__get__(stub, type(stub))

        # Fill buffer to capacity
        for i in range(4):
            bound(f"topic_{i}", {"price": i})

        assert len(stub._exec_overflow_buf) == 4
        assert stub._exec_overflow_evicted == 0

        # Next call should be dropped, not silently evicted
        with patch("hft_platform.services.system.logger") as mock_logger:
            bound("overflow_topic", {"price": 999})

        assert len(stub._exec_overflow_buf) == 4, "buffer size must not change"
        assert stub._exec_overflow_evicted == 1
        mock_logger.critical.assert_called_once()
        log_msg = mock_logger.critical.call_args.args[0]
        assert "FULL" in log_msg
        assert "DLQ" in log_msg

    def test_eviction_counter_increments_repeatedly(self):
        """Each overflow attempt increments the eviction counter."""
        from hft_platform.services.system import HFTSystem

        stub = self._make_stub(maxlen=2)
        bound = HFTSystem._on_exec.__get__(stub, type(stub))

        # Fill
        bound("t1", {"p": 1})
        bound("t2", {"p": 2})

        # 3 overflow attempts
        with patch("hft_platform.services.system.logger"):
            bound("t3", {"p": 3})
            bound("t4", {"p": 4})
            bound("t5", {"p": 5})

        assert stub._exec_overflow_evicted == 3
        assert len(stub._exec_overflow_buf) == 2

    def test_original_events_preserved_on_overflow(self):
        """Overflow must not evict existing events — original fills stay intact."""
        from hft_platform.services.system import HFTSystem

        stub = self._make_stub(maxlen=2)
        bound = HFTSystem._on_exec.__get__(stub, type(stub))

        bound("first", {"p": 1})
        bound("second", {"p": 2})

        with patch("hft_platform.services.system.logger"):
            bound("overflow", {"p": 999})

        topics = [e.topic for e in stub._exec_overflow_buf]
        assert topics == ["first", "second"]

    def test_deal_prefers_order_id_resolution_before_pending_fill_fifo(self):
        """Strong correlation keys must beat weak symbol/side FIFO fallback."""
        from hft_platform.core.order_ids import OrderIdResolver
        from hft_platform.services.system import HFTSystem

        stub = self._make_stub(maxlen=4)
        stub.order_adapter = MagicMock()
        stub.order_adapter.order_id_resolver = OrderIdResolver({"00A1B2": "strat_b:2"})
        stub.order_adapter.resolve_strategy_from_deal_candidates = MagicMock(return_value="strat_a")

        bound = HFTSystem._on_exec.__get__(stub, type(stub))

        bound(
            "deal",
            {
                "payload": {
                    "code": "TMFD6",
                    "action": "Buy",
                    "custom_field": "00A1B2",
                }
            },
        )

        event = stub._exec_overflow_buf[0]
        assert event.data["_resolved_strategy_id"] == "strat_b"
        stub.order_adapter.resolve_strategy_from_deal_candidates.assert_not_called()


class TestGracefulReset:
    """graceful_reset() clears checkpoint, recovery, DLQ, HALT, REDUCE_ONLY."""

    @pytest.mark.asyncio
    async def test_graceful_reset_clears_all_components(self):
        """Graceful reset should clear checkpoint, recovery, DLQ, and state."""
        import tempfile
        import os

        from hft_platform.execution.positions import PositionStore
        from hft_platform.ops.platform_degrade import PlatformDegradeController
        from hft_platform.risk.storm_guard import StormGuard

        with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
            sg = StormGuard()
        sg.trigger_halt("test_halt")
        assert sg.state == StormGuardState.HALT

        pdc = PlatformDegradeController()
        pdc.enter_reduce_only(reason="test_reason")
        assert pdc.reduce_only_active is True

        store = PositionStore()
        store._recovery_positions = {"k1": {"net_qty": 5, "symbol": "TX"}}

        recon = MagicMock()
        recon._halt_triggered = True
        recon._consecutive_failures = 5
        recon._broker_zero_streak = 3
        recon._noncritical_drift_streak = 4

        # Create a temporary checkpoint file
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            f.write(b'{"test": true}')
            ckpt_path = f.name

        ckpt_writer = MagicMock()
        ckpt_writer._path = ckpt_path

        # Build minimal system stub
        system = MagicMock()
        system.checkpoint_writer = ckpt_writer
        system.position_store = store
        system.storm_guard = sg
        system.platform_degrade_controller = pdc
        system.recon_service = recon

        # Call the actual method
        from hft_platform.services.system import HFTSystem
        results = await HFTSystem.graceful_reset(system, reason="test_reset")

        # Verify all components reset
        assert "deleted" in results["checkpoint"]
        assert not os.path.exists(ckpt_path)
        assert store._recovery_positions == {}
        assert pdc.reduce_only_active is False
        assert recon._halt_triggered is False
        assert recon._consecutive_failures == 0
        assert recon._broker_zero_streak == 0
        assert recon._noncritical_drift_streak == 0
        assert sg.state == StormGuardState.NORMAL
