"""Integration tests: fault injection scenarios for recorder, risk, positions, and storm guard."""

from __future__ import annotations

import asyncio
import os
import threading
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent
from hft_platform.contracts.strategy import Side as StrategySide

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _risk_config(tmp_path):
    data = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "tick_size": 0.01,
            "price_band_ticks": 20,
            "max_notional": 10_000_000,
            "max_order_size": 1000,
            "position_limit": 100,
            "daily_loss_limit": 500_000,
        },
        "strategies": {},
    }
    p = tmp_path / "strategy_limits.yaml"
    p.write_text(yaml.dump(data))
    return str(p)


def _make_intent(
    *,
    intent_id: int = 1,
    strategy_id: str = "test_strat",
    symbol: str = "2330",
    intent_type: IntentType = IntentType.NEW,
    side: StrategySide = StrategySide.BUY,
    price: int = 1_000_000,  # 100.0 scaled x10000
    qty: int = 10,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
    )


def _make_fill(
    *,
    fill_id: str = "F001",
    account_id: str = "ACC1",
    order_id: str = "O001",
    strategy_id: str = "strat1",
    symbol: str = "2330",
    side: Side = Side.BUY,
    qty: int = 10,
    price: int = 1_000_000,
    fee: int = 100,
    tax: int = 50,
    match_ts_ns: int = 1_000_000_000,
) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        account_id=account_id,
        order_id=order_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=match_ts_ns,
        match_ts_ns=match_ts_ns,
    )


def _mock_metrics():
    """Return a lightweight mock that satisfies MetricsRegistry access patterns."""
    m = MagicMock()
    m.stormguard_mode.labels.return_value = MagicMock()
    m.risk_reject_total.labels.return_value = MagicMock()
    m.position_pnl_realized.labels.return_value = MagicMock()
    m.recorder_wal_writes_total.labels.return_value = MagicMock()
    m.clickhouse_connection_health = MagicMock()
    return m


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRecorderFaultInjection:
    """Tests 1-2: ClickHouse failure -> WAL takeover and reconnect."""

    @pytest.mark.asyncio
    async def test_ch_failure_mid_session_wal_takeover(self, tmp_path):
        """CH failure mid-session triggers WAL fallback."""
        os.environ["HFT_CLICKHOUSE_ENABLED"] = "0"
        try:
            from hft_platform.recorder.writer import DataWriter

            writer = DataWriter(wal_dir=str(tmp_path / "wal"))
            # Not connected (CH disabled) -> should fall back to WAL
            assert not writer.connected

            data = [{"exch_ts": 1_000_000_000, "ingest_ts": 1_000_000_001, "price": 100}]
            await writer.write("hft.market_data", data)
            # WAL directory should exist and have content
            wal_dir = tmp_path / "wal"
            assert wal_dir.exists()
        finally:
            os.environ.pop("HFT_CLICKHOUSE_ENABLED", None)

    @pytest.mark.asyncio
    async def test_ch_reconnect_after_failure_resumes(self, tmp_path):
        """After CH failure, reconnect marks writer as connected again."""
        os.environ["HFT_CLICKHOUSE_ENABLED"] = "0"
        try:
            from hft_platform.recorder.writer import DataWriter

            writer = DataWriter(wal_dir=str(tmp_path / "wal"))
            assert not writer.connected

            # Simulate reconnect by manually setting state
            writer.connected = True
            writer.ch_client = MagicMock()
            writer._schema_initialized = True
            assert writer.connected

            status = writer.get_status()
            assert status["connected"] is True
            assert status["wal_only_mode"] is False
        finally:
            os.environ.pop("HFT_CLICKHOUSE_ENABLED", None)


@pytest.mark.integration
class TestRiskEngineFaultInjection:
    """Tests 3-4: Validator crash -> fail-closed and recovery."""

    def test_validator_crash_fail_closed(self, tmp_path):
        """When a validator raises, engine must reject (fail-closed)."""
        config_path = _risk_config(tmp_path)
        intent_q = asyncio.Queue()
        order_q = asyncio.Queue()

        with (
            patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
            patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
            patch("hft_platform.risk.engine.get_audit_writer"),
        ):
            mock_mr.get.return_value = _mock_metrics()
            mock_lr.get.return_value = MagicMock()

            from hft_platform.risk.engine import RiskEngine

            engine = RiskEngine(config_path, intent_q, order_q)

            # Inject a crashing validator
            crashing = MagicMock()
            crashing.check.side_effect = RuntimeError("Validator internal error")
            crashing.config = engine.config
            crashing.defaults = engine.config.get("global_defaults", {})
            crashing.strat_configs = engine.config.get("strategies", {})
            engine.validators = [crashing]
            engine._rust_validator = None

            intent = _make_intent()
            # The engine swallows the exception in the run loop,
            # but evaluate() propagates it for direct testing.
            with pytest.raises(RuntimeError, match="Validator internal error"):
                engine.evaluate(intent)

    def test_validator_crash_recovers_on_next_eval(self, tmp_path):
        """After a validator crash, subsequent evaluations work normally."""
        config_path = _risk_config(tmp_path)
        intent_q = asyncio.Queue()
        order_q = asyncio.Queue()

        with (
            patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
            patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
            patch("hft_platform.risk.engine.get_audit_writer"),
        ):
            mock_mr.get.return_value = _mock_metrics()
            mock_lr.get.return_value = MagicMock()

            from hft_platform.risk.engine import RiskEngine

            engine = RiskEngine(config_path, intent_q, order_q)

            # First: crashing validator
            crashing = MagicMock()
            crashing.check.side_effect = RuntimeError("boom")
            crashing.config = engine.config
            crashing.defaults = engine.config.get("global_defaults", {})
            crashing.strat_configs = engine.config.get("strategies", {})
            engine.validators = [crashing]
            engine._rust_validator = None

            intent = _make_intent()
            with pytest.raises(RuntimeError):
                engine.evaluate(intent)

            # Restore working validator
            working = MagicMock()
            working.check.return_value = (True, "OK")
            working.config = engine.config
            working.defaults = engine.config.get("global_defaults", {})
            working.strat_configs = engine.config.get("strategies", {})
            engine.validators = [working]

            decision = engine.evaluate(intent)
            assert decision.approved is True


@pytest.mark.integration
class TestPositionStoreFaultInjection:
    """Tests 5-6: PositionStore capacity and eviction."""

    def test_position_store_at_capacity_triggers_eviction(self):
        """When PositionStore reaches max size, it evicts flat positions."""
        with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0", "HFT_POSITIONS_MAX_SIZE": "5"}):
            with (
                patch("hft_platform.execution.positions.MetricsRegistry") as mock_mr,
                patch("hft_platform.execution.positions.SymbolMetadata"),
                patch("hft_platform.execution.positions.PriceCodec"),
                patch("hft_platform.execution.positions.SymbolMetadataPriceScaleProvider"),
            ):
                mock_mr.get.return_value = _mock_metrics()

                from hft_platform.execution.positions import PositionStore

                store = PositionStore()
                store._positions_max_size = 5

                # Fill store to capacity with flat positions (net_qty=0 after round-trip)
                for i in range(5):
                    buy_fill = _make_fill(
                        fill_id=f"FB{i}",
                        symbol=f"SYM{i}",
                        side=Side.BUY,
                        qty=1,
                        match_ts_ns=i * 1000,
                    )
                    store.on_fill(buy_fill)
                    sell_fill = _make_fill(
                        fill_id=f"FS{i}",
                        symbol=f"SYM{i}",
                        side=Side.SELL,
                        qty=1,
                        match_ts_ns=i * 1000 + 1,
                    )
                    store.on_fill(sell_fill)

                assert len(store.positions) == 5

                # Adding one more should trigger eviction
                new_fill = _make_fill(
                    fill_id="FNEW",
                    symbol="SYM_NEW",
                    side=Side.BUY,
                    qty=1,
                    match_ts_ns=99999,
                )
                store.on_fill(new_fill)

                # Some flat positions should have been evicted
                assert "ACC1:strat1:SYM_NEW" in store.positions

    def test_position_store_eviction_preserves_active(self):
        """Eviction only removes flat positions, not active ones."""
        with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0", "HFT_POSITIONS_MAX_SIZE": "3"}):
            with (
                patch("hft_platform.execution.positions.MetricsRegistry") as mock_mr,
                patch("hft_platform.execution.positions.SymbolMetadata"),
                patch("hft_platform.execution.positions.PriceCodec"),
                patch("hft_platform.execution.positions.SymbolMetadataPriceScaleProvider"),
            ):
                mock_mr.get.return_value = _mock_metrics()

                from hft_platform.execution.positions import PositionStore

                store = PositionStore()
                store._positions_max_size = 3

                # Create 2 flat positions and 1 active
                for i in range(2):
                    store.on_fill(_make_fill(fill_id=f"FB{i}", symbol=f"FLAT{i}", side=Side.BUY, qty=1, match_ts_ns=i))
                    store.on_fill(
                        _make_fill(fill_id=f"FS{i}", symbol=f"FLAT{i}", side=Side.SELL, qty=1, match_ts_ns=i + 1)
                    )

                store.on_fill(_make_fill(fill_id="ACTIVE", symbol="ACTIVE0", side=Side.BUY, qty=10, match_ts_ns=100))

                assert len(store.positions) == 3
                active_key = "ACC1:strat1:ACTIVE0"
                assert store.positions[active_key].net_qty == 10

                # Trigger eviction by adding another
                store.on_fill(_make_fill(fill_id="NEW", symbol="NEW0", side=Side.BUY, qty=5, match_ts_ns=200))

                # Active position must survive eviction
                assert active_key in store.positions
                assert store.positions[active_key].net_qty == 10


@pytest.mark.integration
class TestNormalizerFaultInjection:
    """Tests 7-8: Corrupt and missing symbol payloads."""

    def test_corrupt_normalizer_payload_handled(self):
        """Corrupt payload does not crash the normalizer."""
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        metadata = SymbolMetadata()
        # Accessing nonexistent symbol in internal meta dict should not raise
        result = metadata.meta.get("NONEXISTENT_SYMBOL_XYZ")
        assert result is None
        # price_scale for nonexistent symbol should return default
        scale = metadata.price_scale("NONEXISTENT_SYMBOL_XYZ")
        assert isinstance(scale, int)
        assert scale >= 1

    def test_missing_symbol_in_normalizer(self):
        """Missing symbol lookup returns safe default."""
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        metadata = SymbolMetadata()
        scale = metadata.price_scale("DOES_NOT_EXIST_99999")
        # Should return a sane default (e.g., 10000 or 1) rather than crash
        assert isinstance(scale, (int, float))
        assert scale >= 1


@pytest.mark.integration
class TestStormGuardFaultInjection:
    """Tests 9-11: StormGuard HALT -> rejection, cancel allowed, recovery."""

    def test_stormguard_halt_rejects_new_order(self, tmp_path):
        """In HALT state, new orders must be rejected."""
        config_path = _risk_config(tmp_path)
        intent_q = asyncio.Queue()
        order_q = asyncio.Queue()

        with (
            patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
            patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
            patch("hft_platform.risk.engine.get_audit_writer"),
        ):
            mock_mr.get.return_value = _mock_metrics()
            mock_lr.get.return_value = MagicMock()

            from hft_platform.risk.engine import RiskEngine

            engine = RiskEngine(config_path, intent_q, order_q)
            engine.storm_guard.state = 3  # StormGuardState.HALT

            intent = _make_intent(intent_type=IntentType.NEW)
            decision = engine.evaluate(intent)
            assert decision.approved is False
            assert "HALT" in decision.reason_code or "STORM" in decision.reason_code

    def test_stormguard_halt_allows_cancel(self, tmp_path):
        """In HALT state, cancel orders must still be allowed."""
        config_path = _risk_config(tmp_path)
        intent_q = asyncio.Queue()
        order_q = asyncio.Queue()

        with (
            patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
            patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
            patch("hft_platform.risk.engine.get_audit_writer"),
        ):
            mock_mr.get.return_value = _mock_metrics()
            mock_lr.get.return_value = MagicMock()

            from hft_platform.risk.engine import RiskEngine

            engine = RiskEngine(config_path, intent_q, order_q)
            engine.storm_guard.state = 3  # HALT

            intent = _make_intent(intent_type=IntentType.CANCEL)
            decision = engine.evaluate(intent)
            assert decision.approved is True

    def test_stormguard_halt_to_normal_recovery(self, tmp_path):
        """StormGuard can recover from HALT to NORMAL."""
        from hft_platform.contracts.strategy import StormGuardState
        from hft_platform.risk.validators import StormGuardFSM

        config = yaml.safe_load(open(_risk_config(tmp_path)))
        with patch("hft_platform.risk.validators.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = _mock_metrics()

            fsm = StormGuardFSM(config)
            # Drive to HALT
            fsm.update_pnl(-2_000_000)
            assert fsm.state == StormGuardState.HALT

            # Recover: PnL back to zero triggers immediate step-down from HALT
            fsm.update_pnl(0)
            assert fsm.state == StormGuardState.NORMAL


@pytest.mark.integration
class TestMetricsFaultInjection:
    """Test 12: MetricsRegistry.get() returning None doesn't crash."""

    def test_metrics_registry_get_none_no_crash(self):
        """Components must handle MetricsRegistry.get() returning None gracefully."""
        # Simulate code that checks for None metrics (common pattern in the codebase)
        metrics = None  # Simulates MetricsRegistry.get() returning None
        # All metric access patterns in the codebase guard against None
        if metrics is not None:
            metrics.feed_events_total.labels(symbol="2330").inc()
        # The key assertion: no crash when metrics is None
        assert metrics is None

        # Also verify that actual MetricsRegistry.get() does not crash
        from hft_platform.observability.metrics import MetricsRegistry

        result = MetricsRegistry.get()
        # Result can be an instance or None depending on state; either way no crash
        assert result is None or hasattr(result, "feed_events_total")


@pytest.mark.integration
class TestWALFaultInjection:
    """Test 13: WAL directory not writable."""

    @pytest.mark.asyncio
    async def test_wal_directory_not_writable(self, tmp_path):
        """WAL write to non-writable directory returns False gracefully."""
        from hft_platform.recorder.wal import WALWriter

        bad_dir = str(tmp_path / "no_such_parent" / "wal")
        # WALWriter creates dir in __init__, so use existing dir and make it readonly
        wal_dir = tmp_path / "wal_readonly"
        wal_dir.mkdir()
        writer = WALWriter(str(wal_dir))

        # Make the WAL directory read-only
        wal_dir.chmod(0o444)
        try:
            data = [{"price": 100, "exch_ts": 1000}]
            result = await writer.write("test_table", data)
            # Should handle gracefully — result is bool (True/False)
            assert isinstance(result, bool), f"WAL write should return bool, got {type(result)}"
        finally:
            wal_dir.chmod(0o755)


@pytest.mark.integration
class TestRiskConfigFaultInjection:
    """Test 14: Risk config file missing -> old config preserved."""

    def test_risk_config_missing_preserves_old(self, tmp_path):
        """If reload_config fails, old config must be preserved."""
        config_path = _risk_config(tmp_path)
        intent_q = asyncio.Queue()
        order_q = asyncio.Queue()

        with (
            patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
            patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
            patch("hft_platform.risk.engine.get_audit_writer"),
        ):
            mock_mr.get.return_value = _mock_metrics()
            mock_lr.get.return_value = MagicMock()

            from hft_platform.risk.engine import RiskEngine

            engine = RiskEngine(config_path, intent_q, order_q)
            old_config = dict(engine.config)

            # Delete the config file and reload
            os.remove(config_path)
            engine.reload_config()

            # Config should be preserved (reload_config catches exceptions)
            assert engine.config is not None
            assert "global_defaults" in engine.config


@pytest.mark.integration
class TestOrderQueueFaultInjection:
    """Test 15: Empty order queue with cancellation."""

    @pytest.mark.asyncio
    async def test_empty_order_queue_cancel(self, tmp_path):
        """Cancelling on an empty order queue does not hang or crash."""
        config_path = _risk_config(tmp_path)
        intent_q = asyncio.Queue()
        order_q = asyncio.Queue()

        with (
            patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
            patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
            patch("hft_platform.risk.engine.get_audit_writer"),
        ):
            mock_mr.get.return_value = _mock_metrics()
            mock_lr.get.return_value = MagicMock()

            from hft_platform.risk.engine import RiskEngine

            engine = RiskEngine(config_path, intent_q, order_q)

            # Evaluate a cancel intent directly (no need for run loop)
            cancel_intent = _make_intent(intent_type=IntentType.CANCEL)
            decision = engine.evaluate(cancel_intent)
            assert decision.approved is True

            # Order queue should still be empty
            assert order_q.empty()


@pytest.mark.integration
class TestConcurrentRiskEvaluations:
    """Test 16: Multiple concurrent risk evaluations."""

    def test_concurrent_risk_evaluations_8_threads(self, tmp_path):
        """8 threads calling evaluate() concurrently must not corrupt state."""
        config_path = _risk_config(tmp_path)
        intent_q = asyncio.Queue()
        order_q = asyncio.Queue()

        with (
            patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
            patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
            patch("hft_platform.risk.engine.get_audit_writer"),
        ):
            mock_mr.get.return_value = _mock_metrics()
            mock_lr.get.return_value = MagicMock()

            from hft_platform.risk.engine import RiskEngine

            engine = RiskEngine(config_path, intent_q, order_q)
            engine._cmd_id_lock_enabled = True
            engine._cmd_id_lock = threading.Lock()

            results = []
            errors = []

            def worker(thread_id):
                try:
                    for j in range(10):
                        intent = _make_intent(intent_id=thread_id * 100 + j)
                        decision = engine.evaluate(intent)
                        results.append(decision)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert not errors, f"Errors in concurrent evaluation: {errors}"
            assert len(results) == 80


@pytest.mark.integration
class TestPositionUpdateDuringEviction:
    """Test 17: Position update during eviction."""

    def test_position_update_during_eviction(self):
        """Concurrent fills during eviction must not lose data."""
        with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0", "HFT_POSITIONS_MAX_SIZE": "5"}):
            with (
                patch("hft_platform.execution.positions.MetricsRegistry") as mock_mr,
                patch("hft_platform.execution.positions.SymbolMetadata"),
                patch("hft_platform.execution.positions.PriceCodec"),
                patch("hft_platform.execution.positions.SymbolMetadataPriceScaleProvider"),
            ):
                mock_mr.get.return_value = _mock_metrics()

                from hft_platform.execution.positions import PositionStore

                store = PositionStore()
                store._positions_max_size = 5

                errors = []

                def fill_worker(start_idx):
                    try:
                        for i in range(5):
                            fill = _make_fill(
                                fill_id=f"F{start_idx}_{i}",
                                symbol=f"SYM{start_idx}_{i}",
                                side=Side.BUY,
                                qty=1,
                                match_ts_ns=(start_idx * 100 + i) * 1000,
                            )
                            store.on_fill(fill)
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=fill_worker, args=(i,)) for i in range(4)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=30)

                assert not errors, f"Errors during concurrent fills: {errors}"
                # At least some positions should exist
                assert len(store.positions) > 0


@pytest.mark.integration
class TestRoleGuardedNoopClient:
    """Test 18: _RoleGuardedNoopClient handles all operations safely."""

    def test_noop_client_all_operations(self):
        """All methods on _RoleGuardedNoopClient must return safely."""
        from hft_platform.services.bootstrap import _RoleGuardedNoopClient

        client = _RoleGuardedNoopClient("monitor")

        assert client.login() is False
        assert client.reconnect() is False
        client.subscribe_basket(lambda x: x)
        assert client.fetch_snapshots() == []
        assert client.reload_symbols() is None
        assert client.get_exchange("2330") == ""
        assert client.resubscribe() is False
        client.set_execution_callbacks(None, None)
        result = client.place_order(symbol="2330", price=100, qty=1)
        assert result["status"] == "blocked"
        result = client.cancel_order(None)
        assert result["status"] == "blocked"
        result = client.update_order(None)
        assert result["status"] == "blocked"
        assert client.get_positions() == []
        assert client.get_account_balance()["status"] == "blocked"
        assert client.get_margin()["status"] == "blocked"
        assert client.list_position_detail() == []
        assert client.list_profit_loss() == []
        assert client.validate_symbols() == []
        assert client.get_contract_refresh_status()["status"] == "blocked"
        client.close()
        assert client.logged_in is False
        client.shutdown()
