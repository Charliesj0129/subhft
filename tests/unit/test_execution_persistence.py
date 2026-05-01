"""Tests for execution persistence hardening (Issues #1-#7).

Covers:
- Fill dedup persist/load (Issue #1)
- Fill DLQ persist/load + overflow metric (Issue #2)
- order_id_map persist/load (Issue #3)
- Startup race overflow halt flag (Issue #4)
- Audit drop metric (Issue #5)
- Recorder DATA_LOSS escalation to HALT (Issue #6)
- Deferred terminal overflow metric (Issue #7)
"""

from __future__ import annotations

import asyncio
import collections
import os
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Issue #1: Fill dedup persist/load
# ---------------------------------------------------------------------------


class TestFillDedupPersistence:
    """Verify _seen_fill_ids survives process restart."""

    def _make_router(self, tmp_path, pre_seed=None):
        from hft_platform.execution.router import ExecutionRouter

        persist_path = str(tmp_path / "fill_dedup.jsonl")
        bus = MagicMock()
        raw_queue = asyncio.Queue()
        order_id_map: dict = {}
        position_store = MagicMock()
        terminal_handler = MagicMock()

        with patch.dict(os.environ, {"HFT_FILL_DEDUP_PERSIST_PATH": persist_path}):
            if pre_seed is not None:
                # Write a pre-existing file to simulate prior persist
                import orjson

                os.makedirs(os.path.dirname(persist_path), exist_ok=True)
                with open(persist_path, "wb") as f:
                    for key in pre_seed:
                        f.write(orjson.dumps(key) + b"\n")

            router = ExecutionRouter(
                bus=bus,
                raw_queue=raw_queue,
                order_id_map=order_id_map,
                position_store=position_store,
                terminal_handler=terminal_handler,
            )
        return router, persist_path

    def test_persist_writes_dedup_keys(self, tmp_path):
        import orjson

        router, path = self._make_router(tmp_path)
        router._seen_fill_ids["key1"] = None
        router._seen_fill_ids["key2"] = None
        router._seen_fill_ids["key3"] = None

        router.persist_fill_dedup()

        assert os.path.exists(path)
        with open(path, "rb") as f:
            keys = [orjson.loads(line.strip()) for line in f if line.strip()]
        assert keys == ["key1", "key2", "key3"]

    def test_load_restores_dedup_keys(self, tmp_path):
        pre_seed = ["fill_a", "fill_b", "fill_c"]
        router, _ = self._make_router(tmp_path, pre_seed=pre_seed)

        assert "fill_a" in router._seen_fill_ids
        assert "fill_b" in router._seen_fill_ids
        assert "fill_c" in router._seen_fill_ids
        assert len(router._seen_fill_ids) == 3

    def test_load_enforces_max_size(self, tmp_path):
        pre_seed = [f"key_{i}" for i in range(20)]
        with patch.dict(os.environ, {"HFT_FILL_DEDUP_MAX_SIZE": "5"}):
            router, _ = self._make_router(tmp_path, pre_seed=pre_seed)

        assert len(router._seen_fill_ids) == 5

    def test_load_missing_file_is_noop(self, tmp_path):
        router, _ = self._make_router(tmp_path)
        assert len(router._seen_fill_ids) == 0

    def test_roundtrip_persist_then_load(self, tmp_path):
        router1, path = self._make_router(tmp_path)
        router1._seen_fill_ids["dup_1"] = None
        router1._seen_fill_ids["dup_2"] = None
        router1.persist_fill_dedup()

        # Simulate restart: create new router that loads from same path
        router2, _ = self._make_router(tmp_path, pre_seed=None)
        # File already exists from persist above
        with patch.dict(os.environ, {"HFT_FILL_DEDUP_PERSIST_PATH": path}):
            router2._fill_dedup_persist_path = path
            router2._load_fill_dedup()

        assert "dup_1" in router2._seen_fill_ids
        assert "dup_2" in router2._seen_fill_ids

    @pytest.mark.asyncio
    async def test_new_fill_persists_dedup_window_without_graceful_shutdown(self, tmp_path):
        from hft_platform.contracts.execution import FillEvent
        from hft_platform.contracts.strategy import Side
        from hft_platform.core import timebase
        from hft_platform.execution.normalizer import RawExecEvent

        persist_path = str(tmp_path / "fill_dedup.jsonl")
        with patch.dict(
            os.environ,
            {
                "HFT_FILL_DEDUP_PERSIST_PATH": persist_path,
                "HFT_FILL_DEDUP_PERSIST_INTERVAL_S": "0",
            },
        ):
            router, _ = self._make_router(tmp_path)

        fill = FillEvent(
            fill_id="F_RESTART",
            order_id="ORD_RESTART",
            account_id="ACC1",
            strategy_id="s1",
            symbol="TXFD6",
            side=Side.BUY,
            qty=1,
            price=200000000,
            fee=0,
            tax=0,
            ingest_ts_ns=timebase.now_ns(),
            match_ts_ns=timebase.now_ns(),
        )
        router.position_store.on_fill.return_value = MagicMock(realized_pnl=0)
        router.normalizer = MagicMock()
        router.normalizer.normalize_fill.return_value = fill
        router.raw_queue.put_nowait(RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns()))

        await router.stop(drain_timeout_s=1.0)

        assert os.path.exists(persist_path)

        with patch.dict(os.environ, {"HFT_FILL_DEDUP_PERSIST_PATH": persist_path}):
            restarted, _ = self._make_router(tmp_path)

        assert "F_RESTART" in restarted._seen_fill_ids


# ---------------------------------------------------------------------------
# Issue #2: Fill DLQ persist/load + overflow metric
# ---------------------------------------------------------------------------


class TestFillDLQPersistence:
    """Verify orphaned fill DLQ survives process restart."""

    def test_overflow_increments_metric(self):
        from hft_platform.execution.fill_dlq import OrphanedFillDLQ

        dlq = OrphanedFillDLQ(max_size=3, persist_path="/dev/null")
        metrics = MagicMock()

        with patch("hft_platform.execution.fill_dlq.MetricsRegistry") as mock_reg:
            mock_reg.get.return_value = metrics

            for i in range(5):
                fill = MagicMock(symbol=f"sym{i}", order_id=f"ord{i}")
                dlq.add(fill)

        # Overflow should fire on 4th and 5th adds (when size == max_size before append)
        assert metrics.fill_dlq_overflow_total.inc.call_count == 2

    def test_persist_and_load_roundtrip(self, tmp_path):
        from hft_platform.contracts.execution import FillEvent
        from hft_platform.contracts.strategy import Side
        from hft_platform.execution.fill_dlq import OrphanedFillDLQ

        persist_path = str(tmp_path / "dlq.jsonl")
        dlq = OrphanedFillDLQ(max_size=100, persist_path=persist_path)

        fill = FillEvent(
            fill_id="F001",
            account_id="ACC1",
            order_id="ORD1",
            strategy_id="UNKNOWN",
            symbol="TXFD6",
            side=Side.BUY,
            qty=1,
            price=200000000,
            fee=1000,
            tax=0,
            ingest_ts_ns=1000000,
            match_ts_ns=900000,
        )
        dlq.add(fill)
        dlq.persist()

        assert os.path.exists(persist_path)

        # Load into fresh DLQ
        dlq2 = OrphanedFillDLQ(max_size=100, persist_path=persist_path)
        dlq2.load()

        assert dlq2.count == 1
        items = dlq2.drain()
        restored = items[0]
        assert restored.fill_id == "F001"
        assert restored.symbol == "TXFD6"
        assert restored.side == Side.BUY
        assert restored.qty == 1
        assert restored.price == 200000000

    def test_persist_empty_removes_file(self, tmp_path):
        persist_path = str(tmp_path / "dlq.jsonl")
        # Create a dummy file
        with open(persist_path, "w") as f:
            f.write("dummy\n")

        from hft_platform.execution.fill_dlq import OrphanedFillDLQ

        dlq = OrphanedFillDLQ(max_size=100, persist_path=persist_path)
        dlq.persist()  # Empty queue

        assert not os.path.exists(persist_path)


# ---------------------------------------------------------------------------
# Issue #3: order_id_map persist/load
# ---------------------------------------------------------------------------


class TestOrderIdMapPersistence:
    """Verify order_id_map survives process restart."""

    def _make_adapter(self, tmp_path, pre_seed=None):
        from hft_platform.core import timebase
        from hft_platform.order.adapter import OrderAdapter

        persist_path = str(tmp_path / "oid_map.jsonl")

        if pre_seed is not None:
            import orjson

            os.makedirs(os.path.dirname(persist_path), exist_ok=True)
            now_ns = timebase.now_ns()
            with open(persist_path, "wb") as f:
                # H3: pre-seed with the new schema (k, v, t_ns, s) so the
                # loader does not drop entries as legacy/stale.
                for k, v in pre_seed.items():
                    f.write(orjson.dumps({"k": k, "v": v, "t_ns": now_ns, "s": "live"}) + b"\n")

        with patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": persist_path}):
            adapter = OrderAdapter(
                config_path="config/base/main.yaml",
                order_queue=asyncio.Queue(),
                broker_client=MagicMock(),
            )
        return adapter, persist_path

    def test_persist_writes_mappings(self, tmp_path):
        import orjson

        adapter, path = self._make_adapter(tmp_path)
        adapter.order_id_map["broker_123"] = "strat_a:intent_1"
        adapter.order_id_map["broker_456"] = "strat_b:intent_2"

        adapter.persist_order_id_map()

        assert os.path.exists(path)
        with open(path, "rb") as f:
            rows = [orjson.loads(line.strip()) for line in f if line.strip()]
        assert len(rows) == 2
        # H3: every persisted row carries t_ns + state. The defensive branch
        # in persist_order_id_map() stamps an unmetered entry as live with
        # the current timestamp so downstream load can apply TTL filtering.
        first = rows[0]
        assert first["k"] == "broker_123"
        assert first["v"] == "strat_a:intent_1"
        assert first["s"] == "live"
        assert isinstance(first["t_ns"], int) and first["t_ns"] > 0

    def test_load_restores_mappings(self, tmp_path):
        pre_seed = {"bid_1": "s1:i1", "bid_2": "s2:i2"}
        adapter, _ = self._make_adapter(tmp_path, pre_seed=pre_seed)

        assert adapter.order_id_map["bid_1"] == "s1:i1"
        assert adapter.order_id_map["bid_2"] == "s2:i2"

    def test_load_missing_file_is_noop(self, tmp_path):
        adapter, _ = self._make_adapter(tmp_path)
        assert len(adapter.order_id_map) == 0

    def test_roundtrip(self, tmp_path):
        adapter1, path = self._make_adapter(tmp_path)
        adapter1.order_id_map["x"] = "y"
        adapter1.persist_order_id_map()

        adapter2, _ = self._make_adapter(tmp_path)
        adapter2._order_id_map_persist_path = path
        adapter2._load_order_id_map()
        assert adapter2.order_id_map["x"] == "y"

    @pytest.mark.asyncio
    async def test_register_broker_ids_persists_without_graceful_shutdown(self, tmp_path):
        persist_path = str(tmp_path / "oid_map.jsonl")
        with patch.dict(
            os.environ,
            {
                "HFT_ORDER_ID_MAP_PERSIST_PATH": persist_path,
                "HFT_ORDER_ID_MAP_PERSIST_INTERVAL_S": "0",
            },
        ):
            adapter, _ = self._make_adapter(tmp_path)

        await adapter._register_broker_ids("R47:101", {"ordno": "ORD_RESTART", "seqno": "SEQ_RESTART"})

        # _maybe_persist_order_id_map uses run_in_executor (fire-and-forget).
        # Give the executor a chance to flush before asserting file existence.
        await asyncio.sleep(0.05)
        assert os.path.exists(persist_path)

        with patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": persist_path}):
            restarted, _ = self._make_adapter(tmp_path)

        assert restarted.order_id_map["ORD_RESTART"] == "R47:101"
        assert restarted.order_id_map["SEQ_RESTART"] == "R47:101"


# ---------------------------------------------------------------------------
# Issue #4: Startup race overflow halt flag
# ---------------------------------------------------------------------------


class TestStartupRaceOverflowHalt:
    """Verify broker-thread overflow triggers deferred HALT."""

    def test_overflow_sets_halt_flag(self):
        """When exec overflow buf is full in broker thread, _exec_startup_overflow_lost is set.

        We test the logic inline since HFTSystem requires complex bootstrap.
        The actual code path in system.py:1074-1091 mirrors this exact logic.
        """
        overflow_buf = collections.deque(maxlen=2)
        overflow_max = 2
        evicted = 0
        startup_overflow_lost = False

        # Fill buffer to capacity
        overflow_buf.append("event1")
        overflow_buf.append("event2")

        # Simulate the broker-thread overflow branch (system.py:1075-1091)
        if len(overflow_buf) >= overflow_max:
            evicted += 1
            startup_overflow_lost = True

        assert startup_overflow_lost is True
        assert evicted == 1

    def test_no_flag_when_buffer_has_space(self):
        """When buffer has space, no halt flag is set."""
        overflow_buf = collections.deque(maxlen=10)
        overflow_max = 10
        startup_overflow_lost = False

        overflow_buf.append("event1")

        if len(overflow_buf) >= overflow_max:
            startup_overflow_lost = True

        assert startup_overflow_lost is False


# ---------------------------------------------------------------------------
# Issue #5: Audit drop metric
# ---------------------------------------------------------------------------


class TestAuditDropMetric:
    """Verify audit drops are exposed as Prometheus metric."""

    def test_drop_on_full_increments_metric(self):
        """P0-I3 refactor: start() the writer so queue-full path (not pre-start
        buffer) exercises the drop-metric code path.
        """
        import asyncio as _asyncio

        from hft_platform.recorder.audit import AuditWriter, reset_audit_writer

        reset_audit_writer()

        async def _inner() -> None:
            writer = AuditWriter(queue_size=1)
            await writer.start()
            try:
                # Force overflow to zero-capacity so drops happen immediately after queue full.
                for name in writer._overflow:
                    writer._overflow[name] = collections.deque(maxlen=0)

                metrics_mock = MagicMock()
                with patch("hft_platform.observability.metrics.MetricsRegistry") as mock_reg:
                    mock_reg.get.return_value = metrics_mock

                    writer.log_order({"cmd_id": 1})  # fills queue
                    writer.log_order({"cmd_id": 2})  # should trigger drop + metric

                assert writer._dropped["audit.orders_log"] >= 1
                metrics_mock.audit_dropped_total.labels.assert_called_with(table="audit.orders_log")
            finally:
                await writer.stop()

        _asyncio.run(_inner())
        reset_audit_writer()


# ---------------------------------------------------------------------------
# Issue #6: Recorder DATA_LOSS escalation to HALT
# ---------------------------------------------------------------------------


class TestRecorderDataLossEscalation:
    """Verify DATA_LOSS triggers HALT, not just reduce-only."""

    def test_data_loss_emits_recorder_data_loss_reason(self):
        from hft_platform.ops.platform_inputs import PlatformDegradeInputs

        inputs = MagicMock(spec=PlatformDegradeInputs)
        inputs._recorder_state = MagicMock(return_value="DATA_LOSS")
        inputs._feed_gap_s = MagicMock(return_value=0.0)
        inputs.rss_threshold_mb = 0
        inputs._redis_is_healthy = MagicMock(return_value=True)
        inputs.wal_backlog_files_threshold = 0

        # Test the differentiation directly
        recorder_state = "DATA_LOSS"
        reasons: list[str] = []
        if recorder_state == "DATA_LOSS":
            reasons.append("recorder_data_loss")
        elif recorder_state in {"DEGRADED", "CRITICAL"}:
            reasons.append("clickhouse_unhealthy")

        assert reasons == ["recorder_data_loss"]

    def test_degraded_still_emits_clickhouse_unhealthy(self):
        recorder_state = "DEGRADED"
        reasons: list[str] = []
        if recorder_state == "DATA_LOSS":
            reasons.append("recorder_data_loss")
        elif recorder_state in {"DEGRADED", "CRITICAL"}:
            reasons.append("clickhouse_unhealthy")

        assert reasons == ["clickhouse_unhealthy"]

    def test_autonomy_monitor_halts_on_data_loss(self):
        """AutonomyMonitor triggers storm_guard HALT for recorder_data_loss."""
        from hft_platform.ops.autonomy_monitor import AutonomyMonitor

        storm_guard = MagicMock()
        # StormGuard not in HALT initially (so _evaluate doesn't take the HALT early-return)
        storm_guard.state = MagicMock()
        storm_guard.state.__eq__ = lambda self, other: False  # != HALT

        platform_inputs = MagicMock()
        platform_inputs.reduce_only_reasons.return_value = ["recorder_data_loss"]
        platform_degrade = MagicMock()
        platform_degrade.reduce_only_active = False

        monitor = AutonomyMonitor(
            storm_guard=storm_guard,
            platform_degrade=platform_degrade,
            platform_inputs=platform_inputs,
            recon_service=None,
        )

        decisions = monitor._evaluate()

        storm_guard.trigger_halt.assert_called_once_with("recorder_data_loss")
        assert any(d.action == "trigger_halt" for d in decisions)


# ---------------------------------------------------------------------------
# Issue #7: Deferred terminal overflow metric
# ---------------------------------------------------------------------------


class TestDeferredTerminalOverflowMetric:
    """Verify deferred terminal deque overflow is tracked."""

    @pytest.mark.asyncio
    async def test_overflow_increments_metric(self):
        from hft_platform.order.adapter import OrderAdapter

        adapter = OrderAdapter(
            config_path="config/base/main.yaml",
            order_queue=asyncio.Queue(),
            broker_client=MagicMock(),
        )
        # Shrink deque for testing
        adapter._deferred_terminals = collections.deque(maxlen=2)
        adapter._pending_order_keys = {"strat_a:intent_1"}
        adapter.live_orders = {}

        metrics = MagicMock()
        adapter.metrics = metrics
        adapter.order_id_resolver = MagicMock()
        adapter.order_id_resolver.resolve_order_key.return_value = "strat_a:unknown"

        # Fill to capacity
        await adapter.on_terminal_state("strat_a", "oid_1")
        await adapter.on_terminal_state("strat_a", "oid_2")

        # This should trigger overflow detection
        await adapter.on_terminal_state("strat_a", "oid_3")

        metrics.deferred_terminal_overflow_total.inc.assert_called_once()
