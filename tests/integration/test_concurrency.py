"""Integration tests: concurrency and thread-safety scenarios."""

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


def _make_intent(
    *,
    intent_id: int = 1,
    strategy_id: str = "test_strat",
    symbol: str = "2330",
    intent_type: IntentType = IntentType.NEW,
    side: StrategySide = StrategySide.BUY,
    price: int = 1_000_000,
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


def _mock_metrics():
    m = MagicMock()
    m.stormguard_mode.labels.return_value = MagicMock()
    m.risk_reject_total.labels.return_value = MagicMock()
    m.position_pnl_realized.labels.return_value = MagicMock()
    m.recorder_wal_writes_total.labels.return_value = MagicMock()
    m.clickhouse_connection_health = MagicMock()
    return m


def _make_position_store():
    """Create a Python-backed PositionStore with mocked dependencies."""
    with (
        patch("hft_platform.execution.positions.MetricsRegistry") as mock_mr,
        patch("hft_platform.execution.positions.SymbolMetadata"),
        patch("hft_platform.execution.positions.PriceCodec"),
        patch("hft_platform.execution.positions.SymbolMetadataPriceScaleProvider"),
    ):
        mock_mr.get.return_value = _mock_metrics()
        from hft_platform.execution.positions import PositionStore

        store = PositionStore()
    return store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPositionStoreThreadSafety:
    """Tests 1-2: PositionStore thread safety for same and different symbols."""

    def test_same_symbol_10_threads_10_fills(self):
        """10 threads x 10 fills on same symbol -> net_qty = 100."""
        with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
            store = _make_position_store()
            errors = []

            def worker(thread_id):
                try:
                    for j in range(10):
                        fill = _make_fill(
                            fill_id=f"F{thread_id}_{j}",
                            symbol="2330",
                            side=Side.BUY,
                            qty=1,
                            match_ts_ns=(thread_id * 100 + j) * 1000,
                        )
                        store.on_fill(fill)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert not errors, f"Thread errors: {errors}"
            key = "ACC1:strat1:2330"
            assert key in store.positions
            assert store.positions[key].net_qty == 100

    def test_different_symbols_thread_safety(self):
        """Each thread fills a different symbol concurrently."""
        with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
            store = _make_position_store()
            errors = []

            def worker(thread_id):
                try:
                    symbol = f"SYM{thread_id}"
                    for j in range(10):
                        fill = _make_fill(
                            fill_id=f"F{thread_id}_{j}",
                            symbol=symbol,
                            side=Side.BUY,
                            qty=1,
                            match_ts_ns=(thread_id * 100 + j) * 1000,
                        )
                        store.on_fill(fill)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert not errors
            for i in range(10):
                key = f"ACC1:strat1:SYM{i}"
                assert key in store.positions
                assert store.positions[key].net_qty == 10


@pytest.mark.integration
class TestCmdIdMonotonicity:
    """Tests 3-4: Command ID monotonicity with and without lock."""

    def test_cmd_id_with_lock_20_threads_100_each(self, tmp_path):
        """20 threads x 100 IDs with lock -> all 2000 unique."""
        config_path = _risk_config(tmp_path)
        with patch.dict(os.environ, {"HFT_RISK_CMD_ID_LOCK": "1"}):
            with (
                patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
                patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
                patch("hft_platform.recorder.audit.get_audit_writer"),
            ):
                mock_mr.get.return_value = _mock_metrics()
                mock_lr.get.return_value = MagicMock()

                from hft_platform.risk.engine import RiskEngine

                engine = RiskEngine(config_path, asyncio.Queue(), asyncio.Queue())
                assert engine._cmd_id_lock is not None

                collected_ids: list[int] = []
                lock = threading.Lock()
                errors = []

                def worker():
                    try:
                        local_ids = []
                        for _ in range(100):
                            cid = engine._next_cmd_id()
                            local_ids.append(cid)
                        with lock:
                            collected_ids.extend(local_ids)
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=worker) for _ in range(20)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=30)

                assert not errors
                assert len(collected_ids) == 2000
                assert len(set(collected_ids)) == 2000, "All cmd_ids must be unique"

    def test_cmd_id_sequential_without_lock(self, tmp_path):
        """Sequential generation without lock is monotonically increasing."""
        config_path = _risk_config(tmp_path)
        with patch.dict(os.environ, {"HFT_RISK_CMD_ID_LOCK": "0"}):
            with (
                patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
                patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
                patch("hft_platform.recorder.audit.get_audit_writer"),
            ):
                mock_mr.get.return_value = _mock_metrics()
                mock_lr.get.return_value = MagicMock()

                from hft_platform.risk.engine import RiskEngine

                engine = RiskEngine(config_path, asyncio.Queue(), asyncio.Queue())
                assert engine._cmd_id_lock is None

                ids = [engine._next_cmd_id() for _ in range(100)]
                assert ids == sorted(ids)
                assert len(set(ids)) == 100


@pytest.mark.integration
class TestStormGuardConcurrency:
    """Tests 5-6: StormGuard concurrent reads and transitions."""

    def test_concurrent_reads(self):
        """Multiple threads reading StormGuard state concurrently."""
        from hft_platform.risk.storm_guard import StormGuard, StormGuardState

        with patch("hft_platform.risk.storm_guard.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = _mock_metrics()
            guard = StormGuard()
            guard.state = StormGuardState.NORMAL

            results = []
            errors = []

            def reader():
                try:
                    for _ in range(100):
                        state = guard.state
                        results.append(state)
                        _ = guard.is_safe()
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=reader) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert not errors
            assert len(results) == 1000

    def test_concurrent_transitions(self):
        """Concurrent update() calls must not crash."""
        from hft_platform.risk.storm_guard import StormGuard

        with patch("hft_platform.risk.storm_guard.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = _mock_metrics()
            guard = StormGuard()
            errors = []

            def updater(drawdown_bps):
                try:
                    for _ in range(50):
                        guard.update(drawdown_bps=drawdown_bps)
                except Exception as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=updater, args=(-300,)),  # Try HALT
                threading.Thread(target=updater, args=(-50,)),  # Try WARM
                threading.Thread(target=updater, args=(0,)),  # Try NORMAL
                threading.Thread(target=updater, args=(-150,)),  # Try STORM
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert not errors


@pytest.mark.integration
class TestCallSoonThreadsafe:
    """Test 7: call_soon_threadsafe pattern for cross-thread communication."""

    @pytest.mark.asyncio
    async def test_call_soon_threadsafe_pattern(self):
        """Verify call_soon_threadsafe enqueues items from another thread."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[int] = asyncio.Queue()

        def producer():
            for i in range(10):
                loop.call_soon_threadsafe(queue.put_nowait, i)

        thread = threading.Thread(target=producer)
        thread.start()
        thread.join(timeout=10)

        # Yield control so the event loop processes the scheduled callbacks
        await asyncio.sleep(0.05)

        # Drain queue
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())

        assert len(items) == 10
        assert set(items) == set(range(10))


@pytest.mark.integration
class TestPositionEvictionRace:
    """Test 8: Position eviction under concurrent access."""

    def test_eviction_race(self):
        """Concurrent fills that trigger eviction must not corrupt store."""
        with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0", "HFT_POSITIONS_MAX_SIZE": "10"}):
            store = _make_position_store()
            store._positions_max_size = 10
            errors = []

            def worker(thread_id):
                try:
                    for i in range(20):
                        sym = f"T{thread_id}_S{i}"
                        fill = _make_fill(
                            fill_id=f"F{thread_id}_{i}",
                            symbol=sym,
                            side=Side.BUY,
                            qty=1,
                            match_ts_ns=(thread_id * 1000 + i),
                        )
                        store.on_fill(fill)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert not errors
            assert len(store.positions) > 0


@pytest.mark.integration
class TestRiskEvaluateConcurrent:
    """Test 9: Concurrent risk evaluations."""

    def test_risk_evaluate_concurrent(self, tmp_path):
        """Multiple threads evaluating risk simultaneously."""
        config_path = _risk_config(tmp_path)

        with (
            patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
            patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
            patch("hft_platform.recorder.audit.get_audit_writer"),
        ):
            mock_mr.get.return_value = _mock_metrics()
            mock_lr.get.return_value = MagicMock()

            from hft_platform.risk.engine import RiskEngine

            engine = RiskEngine(config_path, asyncio.Queue(), asyncio.Queue())
            engine._cmd_id_lock_enabled = True
            engine._cmd_id_lock = threading.Lock()

            results = []
            errors = []

            def worker(tid):
                try:
                    for j in range(20):
                        intent = _make_intent(intent_id=tid * 100 + j)
                        decision = engine.evaluate(intent)
                        results.append(decision.approved)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert not errors
            assert len(results) == 100


@pytest.mark.integration
class TestMetricsConcurrentAccess:
    """Test 10: Metrics concurrent access."""

    def test_metrics_concurrent_access(self):
        """MetricsRegistry accessed from multiple threads without crash."""
        from hft_platform.observability.metrics import MetricsRegistry

        errors = []

        def reader():
            try:
                for _ in range(50):
                    m = MetricsRegistry.get()
                    if m is not None:
                        # Access some attributes
                        _ = hasattr(m, "feed_events_total")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors


@pytest.mark.integration
class TestConfigReloadDuringEvaluation:
    """Test 11: Config reload during evaluation."""

    def test_config_reload_during_evaluation(self, tmp_path):
        """Reloading config while evaluating intents must not crash."""
        config_path = _risk_config(tmp_path)

        with (
            patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
            patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
            patch("hft_platform.recorder.audit.get_audit_writer"),
        ):
            mock_mr.get.return_value = _mock_metrics()
            mock_lr.get.return_value = MagicMock()

            from hft_platform.risk.engine import RiskEngine

            engine = RiskEngine(config_path, asyncio.Queue(), asyncio.Queue())
            errors = []

            def evaluator():
                try:
                    for _ in range(50):
                        intent = _make_intent()
                        engine.evaluate(intent)
                except Exception as exc:
                    errors.append(exc)

            def reloader():
                try:
                    for _ in range(10):
                        engine.reload_config()
                except Exception as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=evaluator),
                threading.Thread(target=evaluator),
                threading.Thread(target=reloader),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert not errors


@pytest.mark.integration
class TestQueueConcurrentPutGet:
    """Test 12: Asyncio queue concurrent put/get."""

    @pytest.mark.asyncio
    async def test_queue_concurrent_put_get(self):
        """Multiple async producers and consumers on a bounded queue."""
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=100)
        produced = []
        consumed = []

        async def producer(start):
            for i in range(50):
                await queue.put(start + i)
                produced.append(start + i)

        async def consumer():
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                    consumed.append(item)
                    queue.task_done()
                except asyncio.TimeoutError:
                    break

        # 3 producers, 2 consumers
        producers = [asyncio.create_task(producer(i * 100)) for i in range(3)]
        consumers = [asyncio.create_task(consumer()) for _ in range(2)]

        await asyncio.gather(*producers)
        await asyncio.sleep(0.5)  # Let consumers drain

        for c in consumers:
            c.cancel()
            try:
                await c
            except asyncio.CancelledError:
                pass

        assert len(produced) == 150
        assert len(consumed) >= 100  # At least most should be consumed


@pytest.mark.integration
class TestFillLockContention:
    """Test 13: Fill lock contention with 20 threads x 50 fills."""

    def test_fill_lock_contention_20_threads_50_fills(self):
        """High contention on fill lock must not deadlock or corrupt data."""
        with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
            store = _make_position_store()
            errors = []

            def worker(thread_id):
                try:
                    for j in range(50):
                        fill = _make_fill(
                            fill_id=f"F{thread_id}_{j}",
                            symbol="2330",
                            side=Side.BUY,
                            qty=1,
                            match_ts_ns=(thread_id * 1000 + j),
                        )
                        store.on_fill(fill)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

            assert not errors
            key = "ACC1:strat1:2330"
            assert store.positions[key].net_qty == 1000  # 20 * 50 buys


@pytest.mark.integration
class TestPortfolioAggregatesRace:
    """Test 14: Portfolio aggregates race condition."""

    def test_portfolio_aggregates_race(self):
        """Concurrent fills must maintain consistent portfolio aggregates."""
        with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
            store = _make_position_store()
            errors = []

            def worker(thread_id):
                try:
                    symbol = f"SYM{thread_id}"
                    # Buy then sell to generate realized PnL
                    buy = _make_fill(
                        fill_id=f"BUY{thread_id}",
                        symbol=symbol,
                        side=Side.BUY,
                        qty=10,
                        price=1_000_000,
                        match_ts_ns=thread_id * 1000,
                    )
                    store.on_fill(buy)

                    sell = _make_fill(
                        fill_id=f"SELL{thread_id}",
                        symbol=symbol,
                        side=Side.SELL,
                        qty=10,
                        price=1_100_000,  # Profit
                        match_ts_ns=thread_id * 1000 + 1,
                    )
                    store.on_fill(sell)

                    # Read aggregate (must not crash)
                    _ = store.total_pnl
                    _ = store.get_drawdown_pct()
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert not errors
            # All positions should be flat
            for pos in store.positions.values():
                assert pos.net_qty == 0
            # Total PnL should be positive (all trades were profitable)
            assert store.total_pnl > 0


@pytest.mark.integration
class TestAtomicPositionUpdate:
    """Test 15: No partial state visible during position update."""

    def test_atomic_position_update(self):
        """Readers never see partial state (net_qty updated but avg_price not)."""
        with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
            store = _make_position_store()
            inconsistencies = []
            stop_event = threading.Event()

            def writer():
                for i in range(200):
                    fill = _make_fill(
                        fill_id=f"F{i}",
                        symbol="ATOM",
                        side=Side.BUY,
                        qty=1,
                        price=1_000_000,
                        match_ts_ns=i * 1000,
                    )
                    store.on_fill(fill)
                stop_event.set()

            def reader():
                while not stop_event.is_set():
                    key = "ACC1:strat1:ATOM"
                    pos = store.positions.get(key)
                    if pos is not None:
                        # Snapshot check: if we have qty, avg_price must be set
                        net_qty = pos.net_qty
                        avg_price = pos.avg_price_scaled
                        if net_qty > 0 and avg_price <= 0:
                            inconsistencies.append(f"net_qty={net_qty}, avg_price={avg_price}")

            writer_thread = threading.Thread(target=writer)
            reader_threads = [threading.Thread(target=reader) for _ in range(3)]

            for t in reader_threads:
                t.start()
            writer_thread.start()

            writer_thread.join(timeout=30)
            for t in reader_threads:
                t.join(timeout=5)

            # Fill lock ensures atomic updates, so no inconsistencies expected
            assert not inconsistencies, f"Saw partial state: {inconsistencies[:5]}"
