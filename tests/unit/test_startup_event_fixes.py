"""Tests for startup event fixes: strategy runner start_cursor, exec callback buffering, audit order lifecycle."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_STRATEGY_CONFIG", str(tmp_path / "empty.yaml"))
    (tmp_path / "empty.yaml").write_text("strategies: []\n")
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_RUST", "0")
    monkeypatch.setenv("HFT_STRATEGY_FEATURE_COMPAT_FAIL_FAST", "0")


@pytest.fixture(autouse=True)
def _patch_metrics():
    m = MagicMock()
    m.strategy_latency_ns.labels.return_value = MagicMock()
    m.strategy_intents_total.labels.return_value = MagicMock()
    m.feature_profile_compat_failures_total = MagicMock()
    with patch("hft_platform.strategy.runner.MetricsRegistry") as mr:
        mr.get.return_value = m
        with patch("hft_platform.strategy.runner.LatencyRecorder") as lr:
            lr.get.return_value = MagicMock()
            yield m


# ---------------------------------------------------------------------------
# Fix 1: StrategyRunner replays from start_cursor
# ---------------------------------------------------------------------------


def _make_bus_with_events(events):
    """Create a mock bus that yields events and tracks consume kwargs."""
    bus = MagicMock()
    bus.cursor = len(events) - 1 if events else -1

    captured_kwargs = {}

    async def _consume(**kwargs):
        captured_kwargs.update(kwargs)
        for e in events:
            yield e

    async def _consume_batch(batch_size, **kwargs):
        captured_kwargs.update(kwargs)
        for i in range(0, len(events), batch_size):
            yield events[i : i + batch_size]

    bus.consume = _consume
    bus.consume_batch = _consume_batch
    bus._captured_kwargs = captured_kwargs
    return bus


def _make_runner(bus=None):
    from hft_platform.strategy.runner import StrategyRunner

    bus = bus or MagicMock()
    rq = MagicMock(spec=["put_nowait"])
    rq.put_nowait = MagicMock()
    runner = StrategyRunner(
        bus=bus,
        risk_queue=rq,
        lob_engine=None,
        position_store=None,
        feature_engine=None,
    )
    return runner


class TestStrategyRunnerStartCursor:
    def test_set_start_cursor_stores_value(self):
        runner = _make_runner()
        runner.set_start_cursor(42)
        assert runner._start_cursor == 42

    def test_default_start_cursor_is_none(self):
        runner = _make_runner()
        assert runner._start_cursor is None

    @pytest.mark.asyncio
    async def test_strategy_runner_replays_from_start_cursor(self):
        """Publish events to bus, create runner with start_cursor before those events, verify runner processes them."""
        events = [
            SimpleNamespace(symbol="TSMC", ts=0),
            SimpleNamespace(symbol="2330", ts=0),
        ]
        bus = _make_bus_with_events(events)
        runner = _make_runner(bus=bus)
        runner.set_start_cursor(5)  # cursor before events were published

        # Run with a timeout so it doesn't hang
        run_task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.05)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

        # Verify start_cursor was passed to bus.consume
        assert bus._captured_kwargs.get("start_cursor") == 5
        assert bus._captured_kwargs.get("consumer_name") == "strategy_runner"

    @pytest.mark.asyncio
    async def test_strategy_runner_batch_mode_passes_start_cursor(self, monkeypatch):
        """In batch mode, start_cursor is also forwarded to consume_batch."""
        monkeypatch.setenv("HFT_BUS_BATCH_SIZE", "2")
        events = [SimpleNamespace(symbol="TSMC", ts=0)]
        bus = _make_bus_with_events(events)
        runner = _make_runner(bus=bus)
        runner.set_start_cursor(10)

        run_task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.05)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

        assert bus._captured_kwargs.get("start_cursor") == 10

    @pytest.mark.asyncio
    async def test_strategy_runner_no_start_cursor_uses_none(self):
        """Without set_start_cursor, consume is called with start_cursor=None (default: join at head)."""
        events = [SimpleNamespace(symbol="TSMC", ts=0)]
        bus = _make_bus_with_events(events)
        runner = _make_runner(bus=bus)
        # Do NOT set start_cursor

        run_task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.05)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

        assert bus._captured_kwargs.get("start_cursor") is None


# ---------------------------------------------------------------------------
# Fix 2: exec callback buffers when not running
# ---------------------------------------------------------------------------


class TestExecCallbackBuffersWhenNotRunning:
    def _make_system(self):
        """Minimal HFTSystem-like object with the fields needed by _on_exec."""
        import collections

        sys_obj = SimpleNamespace()
        sys_obj.running = False
        sys_obj.loop = None
        sys_obj._exec_overflow_buf = collections.deque(maxlen=4096)
        sys_obj._EXEC_OVERFLOW_MAX = 4096
        sys_obj._exec_overflow_counter = 0
        sys_obj._exec_overflow_evicted = 0
        sys_obj._exec_startup_overflow_lost = False
        sys_obj.storm_guard = MagicMock()
        sys_obj._persist_lost_exec_event = lambda event: None
        return sys_obj

    def test_exec_callback_buffers_when_not_running(self):
        """When running=False, _on_exec should buffer event to overflow_buf, not drop it."""
        from hft_platform.services.system import HFTSystem

        sys_obj = self._make_system()
        # Call _on_exec as unbound method with our fake system
        HFTSystem._on_exec(sys_obj, "deal", {"payload": {"price": 100}})

        assert len(sys_obj._exec_overflow_buf) == 1
        event = sys_obj._exec_overflow_buf[0]
        assert event.topic == "deal"

    def test_exec_callback_overflow_sets_lost_flag(self):
        """When overflow buf is full and running=False, flag is set for deferred halt."""
        import collections

        from hft_platform.services.system import HFTSystem

        sys_obj = self._make_system()
        sys_obj._exec_overflow_buf = collections.deque(maxlen=2)
        sys_obj._EXEC_OVERFLOW_MAX = 2
        # Fill the buffer
        sys_obj._exec_overflow_buf.append("dummy1")
        sys_obj._exec_overflow_buf.append("dummy2")

        HFTSystem._on_exec(sys_obj, "deal", {"payload": {"price": 200}})

        assert sys_obj._exec_startup_overflow_lost is True
        assert sys_obj._exec_overflow_evicted == 1
        # Buffer should still be at max (not grown)
        assert len(sys_obj._exec_overflow_buf) == 2

    def test_exec_callback_normal_when_running(self):
        """When running=True and loop is set, _on_exec uses call_soon_threadsafe."""
        from hft_platform.services.system import HFTSystem

        sys_obj = self._make_system()
        sys_obj.running = True
        sys_obj.loop = MagicMock()
        sys_obj._safe_enqueue_exec = MagicMock()

        HFTSystem._on_exec(sys_obj, "order", {"state": "filled", "payload": {}})

        # Should have called loop.call_soon_threadsafe
        sys_obj.loop.call_soon_threadsafe.assert_called_once()
        # Buffer should be empty since it went through the normal path
        assert len(sys_obj._exec_overflow_buf) == 0


# ---------------------------------------------------------------------------
# Fix 3: audit log_order called in OrderAdapter dispatch
# ---------------------------------------------------------------------------


class TestOrderDispatchAudit:
    def test_set_audit_writer_stores_writer(self):
        """set_audit_writer injects the audit writer."""

        adapter = self._make_adapter()
        writer = MagicMock()
        adapter.set_audit_writer(writer)
        assert adapter._audit_writer is writer

    def test_audit_log_order_calls_writer(self):
        """_audit_log_order delegates to writer.log_order."""

        adapter = self._make_adapter()
        writer = MagicMock()
        adapter.set_audit_writer(writer)

        adapter._audit_log_order({"event": "dispatched", "symbol": "TSMC"})
        writer.log_order.assert_called_once_with({"event": "dispatched", "symbol": "TSMC"})

    def test_audit_log_order_skips_when_no_writer(self):
        """When no audit writer, _audit_log_order does nothing."""

        adapter = self._make_adapter()
        assert adapter._audit_writer is None
        # Should not raise
        adapter._audit_log_order({"event": "dispatched"})

    def test_audit_log_order_swallows_exception(self):
        """If audit writer raises, _audit_log_order swallows the exception."""

        adapter = self._make_adapter()
        writer = MagicMock()
        writer.log_order.side_effect = RuntimeError("audit broken")
        adapter.set_audit_writer(writer)

        # Should not raise
        adapter._audit_log_order({"event": "dispatched"})

    @pytest.mark.asyncio
    async def test_order_dispatch_creates_audit_entry(self):
        """Dispatch a NEW order and verify audit.log_order is called."""
        from hft_platform.contracts.strategy import IntentType, OrderCommand, OrderIntent, Side

        adapter = self._make_adapter()
        writer = MagicMock()
        adapter.set_audit_writer(writer)

        # Create a minimal NEW order command
        intent = MagicMock(spec=OrderIntent)
        intent.intent_type = IntentType.NEW
        intent.strategy_id = "strat1"
        intent.intent_id = "i1"
        intent.symbol = "TSMC"
        intent.price = 5000000
        intent.qty = 1
        intent.side = Side.BUY
        intent.tif = MagicMock()
        intent.idempotency_key = ""
        intent.price_type = "LMT"

        cmd = MagicMock(spec=OrderCommand)
        cmd.cmd_id = 1
        cmd.created_ns = 100
        cmd.arrival_price = 0
        cmd.decision_price = 5000000
        cmd.intent = intent

        # Mock broker codec and client
        adapter._broker_codec = MagicMock()
        adapter._broker_codec.encode_side.return_value = "Buy"
        adapter._broker_codec.encode_tif.return_value = "ROD"
        adapter._broker_codec.encode_price_type.return_value = "LMT"
        adapter.price_codec = MagicMock()
        adapter.price_codec.descale.return_value = 500.0

        mock_trade = MagicMock()
        adapter.client = MagicMock()

        async def fake_call_api(*args, **kwargs):
            return mock_trade

        adapter._call_api = fake_call_api

        async def _noop(*a, **kw):
            pass

        adapter._register_broker_ids = _noop
        adapter._drain_deferred_terminals = _noop

        await adapter._dispatch_to_api(cmd)

        # Verify audit was called
        assert writer.log_order.called
        call_data = writer.log_order.call_args[0][0]
        assert call_data["event"] == "dispatched"
        assert call_data["intent_type"] == "NEW"
        assert call_data["symbol"] == "TSMC"
        assert call_data["strategy_id"] == "strat1"

    def _make_adapter(self):
        """Create a minimal OrderAdapter for testing audit functionality."""
        import tempfile

        from hft_platform.order.adapter import OrderAdapter

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("symbols: {}\n")
            config_path = f.name

        oq = asyncio.Queue()
        client = MagicMock()
        client.mode = "simulation"

        adapter = OrderAdapter(
            config_path=config_path,
            order_queue=oq,
            broker_client=client,
        )
        return adapter
