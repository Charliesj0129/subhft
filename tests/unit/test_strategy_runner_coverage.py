"""Comprehensive tests for StrategyRunner — strategy dispatch, circuit breaker, obs policy."""

import time
from dataclasses import dataclass
from decimal import Decimal
from unittest.mock import MagicMock

import numpy as np
import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.events import BidAskEvent, LOBStatsEvent, MetaData, TickEvent
from tests.factories import make_bidask_event, make_lob_stats_event, make_tick_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tick(symbol: str = "2330", price: int = 5000000, volume: int = 100) -> TickEvent:
    return make_tick_event(symbol=symbol, price=price, volume=volume, source_ts=1000, local_ts=2000)


def _make_bidask(symbol: str = "2330") -> BidAskEvent:
    bids = np.array([[5000000, 100]], dtype=np.int64)
    asks = np.array([[5010000, 200]], dtype=np.int64)
    return make_bidask_event(symbol=symbol, bids=bids, asks=asks, source_ts=1000, local_ts=2000)


def _make_lobstats(symbol: str = "2330") -> LOBStatsEvent:
    return make_lob_stats_event(
        symbol=symbol,
        ts=1000,
        imbalance=0.1,
        best_bid=5000000,
        best_ask=5010000,
        bid_depth=1000,
        ask_depth=2000,
    )


class DummyStrategy:
    """Minimal strategy stub."""

    def __init__(self, strategy_id: str = "test_strat", enabled: bool = True, symbols=None):
        self.strategy_id = strategy_id
        self.enabled = enabled
        self.symbols = symbols or set()
        self.symbol_tags = []
        self.handle_event_calls: list = []
        self._return_intents: list = []

    def handle_event(self, ctx, event) -> list:
        self.handle_event_calls.append((ctx, event))
        return list(self._return_intents)


class ErrorStrategy(DummyStrategy):
    """Strategy that always raises."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.error_class = RuntimeError
        self.error_msg = "boom"

    def handle_event(self, ctx, event) -> list:
        raise self.error_class(self.error_msg)


class DummyBus:
    """Minimal bus mock with async consume."""

    def __init__(self):
        self._events: list = []

    async def consume(self):
        for ev in self._events:
            yield ev


class DummyRiskQueue:
    def __init__(self):
        self.items: list = []

    def put_nowait(self, item):
        self.items.append(item)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ensure clean env for every test."""
    for key in [
        "HFT_OBS_POLICY",
        "HFT_STRATEGY_CIRCUIT_RUST",
        "HFT_STRATEGY_CIRCUIT_THRESHOLD",
        "HFT_STRATEGY_CIRCUIT_COOLDOWN_S",
        "HFT_STRATEGY_CONFIG",
        "HFT_STRATEGY_METRICS_SAMPLE_EVERY",
        "HFT_STRATEGY_METRICS_BATCH",
        "HFT_TYPED_INTENT_CHANNEL",
        "HFT_STRICT_PRICE_MODE",
        "HFT_STRATEGY_FEATURE_COMPAT_FAIL_FAST",
        "HFT_BUS_BATCH_SIZE",
    ]:
        monkeypatch.delenv(key, raising=False)
    # Disable Rust circuit to test Python FSM
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_RUST", "0")
    # Disable typed intent fast path for simpler testing
    monkeypatch.setenv("HFT_TYPED_INTENT_CHANNEL", "0")
    # Disable feature compat fail-fast
    monkeypatch.setenv("HFT_STRATEGY_FEATURE_COMPAT_FAIL_FAST", "0")


@pytest.fixture
def strategies_yaml(tmp_path):
    cfg = tmp_path / "strategies.yaml"
    cfg.write_text("strategies: []\n")
    return str(cfg)


@pytest.fixture
def runner(strategies_yaml):
    from hft_platform.strategy.runner import StrategyRunner

    bus = DummyBus()
    risk_queue = DummyRiskQueue()
    return StrategyRunner(bus=bus, risk_queue=risk_queue, config_path=strategies_yaml)


# ===========================================================================
# Init Tests
# ===========================================================================


class TestInit:
    def test_basic_init(self, runner):
        assert runner.strategies == []
        assert runner._intent_seq == 0
        assert runner.running is False

    def test_init_with_lob_engine(self, strategies_yaml):
        from hft_platform.strategy.runner import StrategyRunner

        lob = MagicMock()
        lob.get_book_snapshot = MagicMock()
        lob.get_l1_scaled = MagicMock()
        lob.feature_engine = None
        runner = StrategyRunner(DummyBus(), DummyRiskQueue(), lob_engine=lob, config_path=strategies_yaml)
        assert runner._lob_snapshot_source is not None
        assert runner._lob_l1_source is not None

    def test_init_with_feature_engine(self, strategies_yaml):
        from hft_platform.strategy.runner import StrategyRunner

        fe = MagicMock()
        fe.get_feature = MagicMock()
        fe.get_feature_view = MagicMock()
        fe.feature_set_id = MagicMock()
        fe.active_profile_id = MagicMock()
        fe.get_feature_tuple = MagicMock()
        runner = StrategyRunner(DummyBus(), DummyRiskQueue(), feature_engine=fe, config_path=strategies_yaml)
        assert runner._feature_value_source is not None

    def test_init_with_position_store(self, strategies_yaml):
        from hft_platform.strategy.runner import StrategyRunner

        ps = MagicMock()
        ps.positions = {}
        runner = StrategyRunner(DummyBus(), DummyRiskQueue(), position_store=ps, config_path=strategies_yaml)
        assert runner.position_store is ps

    def test_config_from_env(self, tmp_path, monkeypatch):
        from hft_platform.strategy.runner import StrategyRunner

        cfg = tmp_path / "env_strats.yaml"
        cfg.write_text("strategies: []\n")
        monkeypatch.setenv("HFT_STRATEGY_CONFIG", str(cfg))
        runner = StrategyRunner(DummyBus(), DummyRiskQueue(), config_path="nonexistent.yaml")
        assert runner.registry.config_path == str(cfg)

    def test_circuit_threshold_env(self, strategies_yaml, monkeypatch):
        from hft_platform.strategy.runner import StrategyRunner

        monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_THRESHOLD", "20")
        runner = StrategyRunner(DummyBus(), DummyRiskQueue(), config_path=strategies_yaml)
        assert runner._circuit_threshold == 20
        assert runner._circuit_recovery_threshold == 10

    def test_circuit_threshold_non_digit_defaults(self, strategies_yaml, monkeypatch):
        from hft_platform.strategy.runner import StrategyRunner

        monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_THRESHOLD", "abc")
        runner = StrategyRunner(DummyBus(), DummyRiskQueue(), config_path=strategies_yaml)
        assert runner._circuit_threshold == 10  # default


# ===========================================================================
# Obs Policy Tests
# ===========================================================================


class TestObsPolicy:
    def test_obs_policy_minimal(self, strategies_yaml, monkeypatch):
        from hft_platform.strategy.runner import StrategyRunner

        monkeypatch.setenv("HFT_OBS_POLICY", "minimal")
        runner = StrategyRunner(DummyBus(), DummyRiskQueue(), config_path=strategies_yaml)
        assert runner._obs_policy == "minimal"
        assert runner._diagnostic_metrics_enabled is False

    def test_obs_policy_balanced(self, strategies_yaml, monkeypatch):
        from hft_platform.strategy.runner import StrategyRunner

        monkeypatch.setenv("HFT_OBS_POLICY", "balanced")
        runner = StrategyRunner(DummyBus(), DummyRiskQueue(), config_path=strategies_yaml)
        assert runner._obs_policy == "balanced"
        assert runner._diagnostic_metrics_enabled is True

    def test_obs_policy_debug(self, strategies_yaml, monkeypatch):
        from hft_platform.strategy.runner import StrategyRunner

        monkeypatch.setenv("HFT_OBS_POLICY", "debug")
        runner = StrategyRunner(DummyBus(), DummyRiskQueue(), config_path=strategies_yaml)
        assert runner._obs_policy == "debug"

    def test_obs_policy_unknown_returns_empty(self, strategies_yaml, monkeypatch):
        from hft_platform.strategy.runner import StrategyRunner

        monkeypatch.setenv("HFT_OBS_POLICY", "garbage")
        runner = StrategyRunner(DummyBus(), DummyRiskQueue(), config_path=strategies_yaml)
        assert runner._obs_policy == ""

    def test_metrics_sample_every_minimal(self, strategies_yaml, monkeypatch):
        from hft_platform.strategy.runner import StrategyRunner

        monkeypatch.setenv("HFT_OBS_POLICY", "minimal")
        runner = StrategyRunner(DummyBus(), DummyRiskQueue(), config_path=strategies_yaml)
        assert runner._strategy_metrics_sample_every == 8
        assert runner._strategy_metrics_batch == 32

    def test_metrics_sample_every_env_override(self, strategies_yaml, monkeypatch):
        from hft_platform.strategy.runner import StrategyRunner

        monkeypatch.setenv("HFT_STRATEGY_METRICS_SAMPLE_EVERY", "5")
        runner = StrategyRunner(DummyBus(), DummyRiskQueue(), config_path=strategies_yaml)
        assert runner._strategy_metrics_sample_every == 5

    def test_metrics_sample_invalid_defaults_to_1(self, strategies_yaml, monkeypatch):
        from hft_platform.strategy.runner import StrategyRunner

        monkeypatch.setenv("HFT_STRATEGY_METRICS_SAMPLE_EVERY", "not_a_number")
        runner = StrategyRunner(DummyBus(), DummyRiskQueue(), config_path=strategies_yaml)
        assert runner._strategy_metrics_sample_every == 1


# ===========================================================================
# Registration Tests
# ===========================================================================


class TestRegister:
    def test_register_strategy(self, runner):
        strat = DummyStrategy("s1")
        runner.register(strat)
        assert len(runner.strategies) == 1
        assert runner.strategies[0].strategy_id == "s1"

    def test_register_multiple_strategies(self, runner):
        for i in range(5):
            runner.register(DummyStrategy(f"s{i}"))
        assert len(runner.strategies) == 5

    def test_register_builds_executor(self, runner):
        strat = DummyStrategy("s1")
        runner.register(strat)
        assert len(runner._strat_executors) == 1
        assert runner._strat_executors[0][0] is strat

    def test_register_updates_strat_index(self, runner):
        strat = DummyStrategy("s1")
        runner.register(strat)
        assert "s1" in runner._strat_index
        assert 0 in runner._strat_index["s1"]

    def test_register_initializes_metric_counters(self, runner):
        runner.register(DummyStrategy("s1"))
        assert "s1" in runner._strategy_metrics_seq
        assert runner._strategy_pending_intents["s1"] == 0

    def test_resolve_symbols_from_list(self, runner):
        strat = DummyStrategy("s1", symbols=["2330", "2317"])
        runner.register(strat)
        assert "2330" in strat.symbols
        assert "2317" in strat.symbols

    def test_resolve_symbols_with_tags(self, runner):
        strat = DummyStrategy("s1", symbols=["tag:futures"])
        runner.register(strat)
        # Tag resolution depends on SymbolMetadata — empty metadata yields empty set
        assert isinstance(strat.symbols, set)


# ===========================================================================
# Intent Factory Tests
# ===========================================================================


class TestIntentFactory:
    def test_intent_factory_creates_order_intent(self, runner):
        intent = runner._intent_factory(
            strategy_id="s1",
            symbol="2330",
            side=Side.BUY,
            price=5000000,
            qty=10,
            tif=TIF.LIMIT,
            intent_type=IntentType.NEW,
        )
        assert isinstance(intent, OrderIntent)
        assert intent.intent_id == 1
        assert intent.strategy_id == "s1"
        assert intent.symbol == "2330"
        assert intent.side == Side.BUY
        assert intent.price == 5000000
        assert intent.qty == 10

    def test_intent_factory_increments_seq(self, runner):
        for i in range(5):
            intent = runner._intent_factory("s1", "2330", Side.BUY, 5000000, 10, TIF.LIMIT, IntentType.NEW)
        assert runner._intent_seq == 5
        assert intent.intent_id == 5

    def test_intent_factory_uses_current_source_ts(self, runner):
        runner._current_source_ts_ns = 999999
        intent = runner._intent_factory("s1", "2330", Side.BUY, 5000000, 10, TIF.LIMIT, IntentType.NEW)
        assert intent.source_ts_ns == 999999

    def test_intent_factory_explicit_source_ts(self, runner):
        runner._current_source_ts_ns = 111
        intent = runner._intent_factory(
            "s1",
            "2330",
            Side.BUY,
            5000000,
            10,
            TIF.LIMIT,
            IntentType.NEW,
            source_ts_ns=222,
        )
        assert intent.source_ts_ns == 222

    def test_intent_factory_cancel_with_target(self, runner):
        intent = runner._intent_factory(
            "s1",
            "2330",
            Side.BUY,
            0,
            0,
            TIF.LIMIT,
            IntentType.CANCEL,
            target_order_id="order_123",
        )
        assert intent.intent_type == IntentType.CANCEL
        assert intent.target_order_id == "order_123"

    def test_typed_intent_fastpath(self, runner, monkeypatch):
        """When typed intent is enabled, factory returns tuple instead of OrderIntent."""
        monkeypatch.setenv("HFT_TYPED_INTENT_CHANNEL", "1")
        # Re-init risk submit settings
        runner._typed_intent_fastpath = True
        result = runner._intent_factory("s1", "2330", Side.BUY, 5000000, 10, TIF.LIMIT, IntentType.NEW)
        assert isinstance(result, tuple)
        assert result[0] == "typed_intent_v1"


# ===========================================================================
# process_event Tests
# ===========================================================================


class TestProcessEvent:
    @pytest.mark.asyncio
    async def test_process_tick_event(self, runner):
        strat = DummyStrategy("s1")
        runner.register(strat)
        tick = _make_tick()
        await runner.process_event(tick)
        assert len(strat.handle_event_calls) == 1
        ctx, ev = strat.handle_event_calls[0]
        assert ev is tick

    @pytest.mark.asyncio
    async def test_process_bidask_event(self, runner):
        strat = DummyStrategy("s1")
        runner.register(strat)
        ba = _make_bidask()
        await runner.process_event(ba)
        assert len(strat.handle_event_calls) == 1

    @pytest.mark.asyncio
    async def test_process_lobstats_event(self, runner):
        strat = DummyStrategy("s1")
        runner.register(strat)
        lob = _make_lobstats()
        await runner.process_event(lob)
        assert len(strat.handle_event_calls) == 1

    @pytest.mark.asyncio
    async def test_strategy_returns_intents_submitted_to_risk(self, runner):
        strat = DummyStrategy("s1")
        intent = OrderIntent(
            intent_id=1,
            strategy_id="s1",
            symbol="2330",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=5000000,
            qty=10,
            tif=TIF.LIMIT,
        )
        strat._return_intents = [intent]
        runner.register(strat)
        await runner.process_event(_make_tick())
        assert len(runner.risk_queue.items) == 1
        assert runner.risk_queue.items[0] is intent

    @pytest.mark.asyncio
    async def test_disabled_strategy_skipped(self, runner):
        strat = DummyStrategy("s1", enabled=False)
        runner.register(strat)
        await runner.process_event(_make_tick())
        assert len(strat.handle_event_calls) == 0

    @pytest.mark.asyncio
    async def test_multiple_strategies_all_invoked(self, runner):
        s1 = DummyStrategy("s1")
        s2 = DummyStrategy("s2")
        runner.register(s1)
        runner.register(s2)
        await runner.process_event(_make_tick())
        assert len(s1.handle_event_calls) == 1
        assert len(s2.handle_event_calls) == 1

    @pytest.mark.asyncio
    async def test_targeted_dispatch_by_strategy_id(self, runner):
        s1 = DummyStrategy("s1")
        s2 = DummyStrategy("s2")
        runner.register(s1)
        runner.register(s2)

        # Create a simple event with strategy_id attribute (TickEvent has __slots__)
        @dataclass
        class TargetedEvent:
            strategy_id: str = "s1"
            symbol: str = "2330"

        await runner.process_event(TargetedEvent())
        assert len(s1.handle_event_calls) == 1
        assert len(s2.handle_event_calls) == 0

    @pytest.mark.asyncio
    async def test_position_delta_invalidates_cache(self, runner):
        strat = DummyStrategy("s1")
        runner.register(strat)
        runner._positions_dirty = False

        @dataclass
        class FakeDelta:
            delta_source: str = "FILL"
            symbol: str = "2330"

        await runner.process_event(FakeDelta())
        # Positions should have been rebuilt
        assert runner._positions_dirty is False  # rebuilt inside process_event

    @pytest.mark.asyncio
    async def test_extract_trace_from_meta(self, runner):
        strat = DummyStrategy("s1")
        runner.register(strat)
        meta = MetaData(seq=42, source_ts=1000, local_ts=2000, topic="tick")
        tick = TickEvent(meta=meta, symbol="2330", price=5000000, volume=100)
        await runner.process_event(tick)
        assert runner._current_trace_id == "tick:42"
        assert runner._current_source_ts_ns == 2000

    @pytest.mark.asyncio
    async def test_extract_trace_from_ts_attr(self, runner):
        strat = DummyStrategy("s1")
        runner.register(strat)
        lob = _make_lobstats()
        await runner.process_event(lob)
        assert runner._current_source_ts_ns == 1000  # ts field


# ===========================================================================
# Circuit Breaker FSM Tests (Python path)
# ===========================================================================


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_normal_to_degraded(self, runner, monkeypatch):
        monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_THRESHOLD", "4")
        runner._circuit_threshold = 4
        runner._circuit_recovery_threshold = 2

        strat = ErrorStrategy("s1")
        runner.register(strat)

        for _ in range(2):  # half of 4 = 2
            await runner.process_event(_make_tick())

        assert runner._circuit_states.get("s1") == "degraded"
        assert strat.enabled is True  # not halted yet

    @pytest.mark.asyncio
    async def test_degraded_to_halted(self, runner):
        runner._circuit_threshold = 4
        runner._circuit_recovery_threshold = 2

        strat = ErrorStrategy("s1")
        runner.register(strat)

        for _ in range(4):
            await runner.process_event(_make_tick())

        assert runner._circuit_states.get("s1") == "halted"
        assert strat.enabled is False

    @pytest.mark.asyncio
    async def test_halted_strategy_skipped(self, runner):
        runner._circuit_threshold = 2
        runner._circuit_recovery_threshold = 1

        strat = ErrorStrategy("s1")
        runner.register(strat)

        # Trigger halt
        for _ in range(2):
            await runner.process_event(_make_tick())
        assert strat.enabled is False

        # Now swap to a non-error strategy to prove it's skipped
        good = DummyStrategy("s1", enabled=False)
        runner.strategies[0] = good
        runner._rebuild_executors()
        await runner.process_event(_make_tick())
        assert len(good.handle_event_calls) == 0

    @pytest.mark.asyncio
    async def test_cooldown_recovery(self, runner, monkeypatch):
        runner._circuit_threshold = 2
        runner._circuit_recovery_threshold = 100  # High so it stays degraded
        runner._circuit_cooldown_ns = 1  # 1ns cooldown for test

        strat = ErrorStrategy("s1")
        runner.register(strat)

        for _ in range(2):
            await runner.process_event(_make_tick())
        assert strat.enabled is False
        assert runner._circuit_states["s1"] == "halted"

        # Advance time (halted_at is already in the past with 1ns cooldown)
        time.sleep(0.001)
        # Replace with a working strategy to see recovery
        good = DummyStrategy("s1", enabled=False)
        runner.strategies[0] = good
        runner._rebuild_executors()
        await runner.process_event(_make_tick())
        assert good.enabled is True
        # Re-enabled in degraded state
        assert runner._circuit_states["s1"] == "degraded"

    @pytest.mark.asyncio
    async def test_degraded_recovery_on_success(self, runner):
        runner._circuit_threshold = 4
        runner._circuit_recovery_threshold = 2

        strat = ErrorStrategy("s1")
        runner.register(strat)

        # Go to degraded
        for _ in range(2):
            await runner.process_event(_make_tick())
        assert runner._circuit_states["s1"] == "degraded"

        # Switch to good strategy
        good = DummyStrategy("s1")
        runner.strategies[0] = good
        runner._rebuild_executors()

        # Need recovery_threshold consecutive successes
        for _ in range(2):
            await runner.process_event(_make_tick())

        assert runner._circuit_states["s1"] == "normal"
        assert runner._failure_counts["s1"] == 0

    @pytest.mark.asyncio
    async def test_exception_resets_success_count(self, runner):
        runner._circuit_threshold = 10
        runner._circuit_recovery_threshold = 5

        strat = ErrorStrategy("s1")
        runner.register(strat)

        await runner.process_event(_make_tick())
        assert runner._circuit_success_counts.get("s1", 0) == 0


# ===========================================================================
# Error Handling Tests
# ===========================================================================


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_strategy_exception_returns_empty_intents(self, runner):
        strat = ErrorStrategy("s1")
        runner.register(strat)
        await runner.process_event(_make_tick())
        # No intents submitted
        assert len(runner.risk_queue.items) == 0

    @pytest.mark.asyncio
    async def test_strategy_exception_does_not_crash_runner(self, runner):
        strat = ErrorStrategy("s1")
        runner.register(strat)
        # Should not raise
        await runner.process_event(_make_tick())
        await runner.process_event(_make_tick())
        # Failure count incremented
        assert runner._failure_counts.get("s1", 0) == 2

    @pytest.mark.asyncio
    async def test_multiple_strategies_one_errors(self, runner):
        error_strat = ErrorStrategy("s1")
        good_strat = DummyStrategy("s2")
        runner.register(error_strat)
        runner.register(good_strat)
        await runner.process_event(_make_tick())
        # Good strategy still gets called
        assert len(good_strat.handle_event_calls) == 1


# ===========================================================================
# Position Building Tests
# ===========================================================================


class TestPositionBuilding:
    def test_no_position_store(self, runner):
        result = runner._build_positions_by_strategy()
        assert result == {}

    def test_position_store_with_keyed_positions(self, runner):
        @dataclass
        class FakePos:
            net_qty: int = 10

        store = MagicMock()
        store._rust_tracker = None  # Disable Rust fast path
        store.positions = {"pos:s1:2330": FakePos(10), "pos:s2:2317": FakePos(5)}
        runner.position_store = store
        result = runner._build_positions_by_strategy()
        assert result["s1"]["2330"] == 10
        assert result["s2"]["2317"] == 5

    def test_position_store_with_structured_positions(self, runner):
        @dataclass
        class StructPos:
            strategy_id: str
            symbol: str
            net_qty: int

        store = MagicMock()
        store._rust_tracker = None
        store.positions = {"k1": StructPos("s1", "2330", 10)}
        runner.position_store = store
        result = runner._build_positions_by_strategy()
        assert result["s1"]["2330"] == 10

    def test_position_store_fallback_keys(self, runner):
        @dataclass
        class FakePos:
            net_qty: int = 3

        store = MagicMock()
        store._rust_tracker = None
        store.positions = {"simple_key": FakePos(3)}
        runner.position_store = store
        result = runner._build_positions_by_strategy()
        assert result.get("*", {}).get("simple_key") == 3

    def test_invalidate_positions(self, runner):
        runner._positions_dirty = False
        runner.invalidate_positions()
        assert runner._positions_dirty is True

    def test_position_key_cache(self, runner):
        @dataclass
        class FakePos:
            net_qty: int = 10

        store = MagicMock()
        store._rust_tracker = None
        store.positions = {"pos:s1:2330": FakePos(10)}
        runner.position_store = store
        runner._build_positions_by_strategy()
        assert "pos:s1:2330" in runner._position_key_cache


# ===========================================================================
# Run Loop Tests
# ===========================================================================


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_run_consumes_events(self, runner):
        strat = DummyStrategy("s1")
        runner.register(strat)
        runner.bus._events = [_make_tick(), _make_tick()]

        await runner.run()
        assert len(strat.handle_event_calls) == 2
        assert runner.running is True

    @pytest.mark.asyncio
    async def test_run_sets_running(self, runner):
        runner.bus._events = []
        await runner.run()
        assert runner.running is True


# ===========================================================================
# Executor Sync Tests
# ===========================================================================


class TestExecutorSync:
    def test_executors_match_strategy_list(self, runner):
        strat = DummyStrategy("s1")
        runner.register(strat)
        assert runner._executors_match_strategy_list() is True

    def test_executors_mismatch_triggers_rebuild(self, runner):
        strat = DummyStrategy("s1")
        runner.register(strat)
        runner.strategies.append(DummyStrategy("s2"))
        assert runner._executors_match_strategy_list() is False

    @pytest.mark.asyncio
    async def test_rebuild_on_mismatch(self, runner):
        s1 = DummyStrategy("s1")
        runner.register(s1)
        s2 = DummyStrategy("s2")
        runner.strategies.append(s2)
        # process_event should rebuild
        await runner.process_event(_make_tick())
        assert len(runner._strat_executors) == 2


# ===========================================================================
# Flush Pending Metrics Tests
# ===========================================================================


class TestFlushPendingMetrics:
    def test_flush_clears_pending_when_no_metrics(self, runner):
        runner.metrics = None
        runner._strategy_pending_intents["s1"] = 5
        runner._flush_pending_strategy_metrics()
        assert runner._strategy_pending_intents.get("s1") is None or runner._strategy_pending_intents == {}

    def test_flush_with_metrics(self, runner):
        strat = DummyStrategy("s1")
        runner.register(strat)
        runner._strategy_pending_intents["s1"] = 3
        runner._flush_pending_strategy_metrics()
        assert runner._strategy_pending_intents["s1"] == 0


# ===========================================================================
# Scale Price Tests
# ===========================================================================


class TestScalePrice:
    def test_scale_price_int(self, runner):
        # Default scale factor (10000)
        result = runner._scale_price("2330", 100)
        assert isinstance(result, int)

    def test_scale_price_decimal(self, runner):
        result = runner._scale_price("2330", Decimal("100.5"))
        assert isinstance(result, int)
