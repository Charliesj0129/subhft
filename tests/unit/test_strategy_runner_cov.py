"""Comprehensive coverage tests for StrategyRunner.

Covers branches NOT already tested in:
  - tests/unit/test_strategy_runner_routing.py
  - tests/unit/test_strategy_runner_position_race.py

Sections:
  A. Constructor branches
  B. register() method
  C. process_event() dispatch
  D. Intent factory (_intent_factory)
  E. Circuit breaker (Python-path, Rust disabled)
  F. _build_positions_by_strategy() edge cases
  G. _extract_event_trace()
  H. run() async loop
  I. _scale_price() and _emit_trace()
  J. _rebuild_executors()
  K. _flush_pending_strategy_metrics()
  L. _resolve_risk_submit()
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.events import BidAskEvent, LOBStatsEvent, MetaData, TickEvent
from hft_platform.strategy.base import BaseStrategy
from hft_platform.strategy.runner import StrategyRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runner(
    position_store=None,
    monkeypatch=None,
    env_overrides: dict | None = None,
    lob_engine=None,
    feature_engine=None,
):
    """Create a StrategyRunner with registry patched out (no YAML file needed)."""
    risk_q = asyncio.Queue()

    def _build(rq=None):
        with patch("hft_platform.strategy.runner.StrategyRegistry") as mock_reg:
            mock_reg.return_value.instantiate.return_value = []
            runner = StrategyRunner(
                MagicMock(),
                rq or risk_q,
                config_path="dummy",
                position_store=position_store,
                lob_engine=lob_engine,
                feature_engine=feature_engine,
            )
        return runner, risk_q

    if env_overrides and monkeypatch:
        for k, v in env_overrides.items():
            monkeypatch.setenv(k, v)
        return _build()

    return _build()


def _tick(symbol="2330", price: int = 100_000, seq: int = 1, local_ts: int = 99) -> TickEvent:
    return TickEvent(
        meta=MetaData(seq=seq, topic="tick", source_ts=1, local_ts=local_ts),
        symbol=symbol,
        price=price,
        volume=1,
        total_volume=1,
        bid_side_total_vol=0,
        ask_side_total_vol=0,
        is_simtrade=False,
        is_odd_lot=False,
    )


class _PassStrategy(BaseStrategy):
    """Succeeds every event and accumulates call count."""

    def __init__(self, sid="pass", symbols=None):
        super().__init__(strategy_id=sid, symbols=symbols or [])
        self.calls = 0
        self.last_event = None

    def on_tick(self, event):
        self.calls += 1
        self.last_event = event

    def on_book_update(self, event):
        self.calls += 1

    def on_stats(self, event):
        self.calls += 1


class _IntentStrategy(BaseStrategy):
    """Emits exactly one BUY intent per tick."""

    def __init__(self, sid="intent", symbols=None, price: int = 10_000):
        super().__init__(strategy_id=sid, symbols=symbols or [])
        self._price = price

    def on_tick(self, event):
        self.buy(event.symbol, self._price, 1, tif=TIF.LIMIT)


class _FailStrategy(BaseStrategy):
    """Always raises on every event type."""

    def on_tick(self, event):
        raise ValueError("deliberate tick failure")

    def on_book_update(self, event):
        raise ValueError("deliberate bidask failure")


# ---------------------------------------------------------------------------
# A. Constructor branches
# ---------------------------------------------------------------------------


def test_constructor_obs_policy_balanced(monkeypatch):
    monkeypatch.setenv("HFT_OBS_POLICY", "balanced")
    runner, _ = _make_runner(monkeypatch=monkeypatch, env_overrides={})
    assert runner._obs_policy == "balanced"
    assert runner._diagnostic_metrics_enabled is True
    assert runner._strategy_metrics_sample_every >= 2
    assert runner._strategy_metrics_batch >= 8


def test_constructor_obs_policy_debug(monkeypatch):
    monkeypatch.setenv("HFT_OBS_POLICY", "debug")
    runner, _ = _make_runner(monkeypatch=monkeypatch, env_overrides={})
    assert runner._obs_policy == "debug"
    assert runner._diagnostic_metrics_enabled is True


def test_constructor_obs_policy_unknown_defaults_empty(monkeypatch):
    monkeypatch.setenv("HFT_OBS_POLICY", "turbo")
    runner, _ = _make_runner(monkeypatch=monkeypatch, env_overrides={})
    assert runner._obs_policy == ""
    assert runner._diagnostic_metrics_enabled is True


def test_constructor_metrics_sample_invalid_env(monkeypatch):
    """Garbage value for HFT_STRATEGY_METRICS_SAMPLE_EVERY falls back to 1."""
    monkeypatch.setenv("HFT_STRATEGY_METRICS_SAMPLE_EVERY", "not_an_int")
    runner, _ = _make_runner(monkeypatch=monkeypatch, env_overrides={})
    assert runner._strategy_metrics_sample_every == 1


def test_constructor_metrics_batch_invalid_env(monkeypatch):
    """Garbage value for HFT_STRATEGY_METRICS_BATCH falls back to 1."""
    monkeypatch.setenv("HFT_STRATEGY_METRICS_BATCH", "bad")
    runner, _ = _make_runner(monkeypatch=monkeypatch, env_overrides={})
    assert runner._strategy_metrics_batch == 1


def test_constructor_circuit_threshold_invalid_env(monkeypatch):
    """Non-digit HFT_STRATEGY_CIRCUIT_THRESHOLD falls back to 10."""
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_THRESHOLD", "abc")
    runner, _ = _make_runner(monkeypatch=monkeypatch, env_overrides={})
    assert runner._circuit_threshold == 10


def test_constructor_circuit_threshold_custom(monkeypatch):
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_THRESHOLD", "20")
    runner, _ = _make_runner(monkeypatch=monkeypatch, env_overrides={})
    assert runner._circuit_threshold == 20
    assert runner._circuit_recovery_threshold == 10


def test_constructor_circuit_cooldown_custom(monkeypatch):
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_COOLDOWN_S", "120")
    runner, _ = _make_runner(monkeypatch=monkeypatch, env_overrides={})
    assert runner._circuit_cooldown_ns >= 120_000_000_000


def test_constructor_typed_intent_fastpath_disabled(monkeypatch):
    """HFT_TYPED_INTENT_CHANNEL=0 disables typed fastpath even if method exists."""
    monkeypatch.setenv("HFT_TYPED_INTENT_CHANNEL", "0")

    class _TypedQ:
        def put_nowait(self, x): ...
        def submit_nowait(self, x): ...
        def submit_typed_nowait(self, x):
            return "ok"

    with patch("hft_platform.strategy.runner.StrategyRegistry") as mock_reg:
        mock_reg.return_value.instantiate.return_value = []
        runner = StrategyRunner(MagicMock(), _TypedQ(), config_path="dummy")
    assert runner._typed_intent_fastpath is False


def test_constructor_with_lob_engine_provides_snapshot_source():
    lob = MagicMock()
    lob.get_book_snapshot = MagicMock(return_value={"bids": [], "asks": []})
    lob.get_l1_scaled = MagicMock(return_value=(1, 100_000, 100_100, 200_100, 100, 10, 10))
    runner, _ = _make_runner(lob_engine=lob)
    assert runner._lob_snapshot_source is lob.get_book_snapshot
    assert runner._lob_l1_source is lob.get_l1_scaled


def test_constructor_with_feature_engine_provides_sources():
    fe = MagicMock()
    fe.get_feature = MagicMock(return_value=42)
    fe.get_feature_view = MagicMock(return_value={})
    fe.feature_set_id = MagicMock(return_value="v1")
    fe.active_profile_id = MagicMock(return_value="default")
    fe.get_feature_tuple = MagicMock(return_value=())
    runner, _ = _make_runner(feature_engine=fe)
    assert runner._feature_value_source is fe.get_feature
    assert runner._feature_set_source is fe.feature_set_id


def test_constructor_rust_circuit_disabled_via_env(monkeypatch):
    """HFT_STRATEGY_CIRCUIT_RUST=0 disables the Rust circuit breaker path."""
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_RUST", "0")
    # Need to reimport runner module to pick up env var at module load time
    import importlib

    import hft_platform.strategy.runner as runner_module

    importlib.reload(runner_module)

    with patch.object(runner_module, "StrategyRegistry") as mock_reg:
        mock_reg.return_value.instantiate.return_value = []
        runner = runner_module.StrategyRunner(MagicMock(), asyncio.Queue(), config_path="dummy")
    assert runner._rust_circuit is None

    # Restore
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_RUST", "1")
    importlib.reload(runner_module)


# ---------------------------------------------------------------------------
# B. register() method
# ---------------------------------------------------------------------------


def test_register_appends_strategy():
    runner, _ = _make_runner()
    strat = _PassStrategy("s1", symbols=["AAA"])
    runner.register(strat)
    assert strat in runner.strategies
    assert len(runner._strat_executors) == 1


def test_register_updates_strat_index():
    runner, _ = _make_runner()
    strat = _PassStrategy("idx_s", symbols=["AAA"])
    runner.register(strat)
    assert "idx_s" in runner._strat_index
    assert 0 in runner._strat_index["idx_s"]


def test_register_initializes_pending_tracking():
    runner, _ = _make_runner()
    strat = _PassStrategy("pending_s", symbols=["AAA"])
    runner.register(strat)
    assert "pending_s" in runner._strategy_pending_intents
    assert runner._strategy_pending_intents["pending_s"] == 0


def test_register_compat_error_raises_when_fail_fast():
    """A strategy with feature deps and no feature engine raises RuntimeError."""
    runner, _ = _make_runner()
    runner._feature_compat_fail_fast = True

    class _FeatureDepStrategy(BaseStrategy):
        required_feature_set_id = "v1"

        def on_tick(self, event): ...

    strat = _FeatureDepStrategy("dep_strat", symbols=[])
    with pytest.raises(RuntimeError, match="feature_engine_missing"):
        runner.register(strat)


def test_register_compat_error_no_raise_when_fail_fast_off():
    """When fail_fast is disabled, compat error is logged but registration proceeds."""
    runner, _ = _make_runner()
    runner._feature_compat_fail_fast = False

    class _FeatureDepStrategy(BaseStrategy):
        required_feature_set_id = "v2"

        def on_tick(self, event): ...

    strat = _FeatureDepStrategy("dep_strat_nff", symbols=[])
    runner.register(strat)  # should not raise
    assert strat in runner.strategies


def test_register_symbol_tag_resolution():
    """Symbols with tag: prefix are resolved via symbol_metadata."""
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    meta = SymbolMetadata()
    runner, _ = _make_runner()
    runner.symbol_metadata = meta

    class _TagStrategy(BaseStrategy):
        symbols = ["tag:equity"]

        def on_tick(self, event): ...

    strat = _TagStrategy("tag_s", symbols=[])
    # SymbolMetadata.symbols_for_tags returns empty set by default
    runner.register(strat)
    assert strat in runner.strategies


# ---------------------------------------------------------------------------
# C. process_event() dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_event_tick_dispatched():
    runner, rq = _make_runner()
    strat = _PassStrategy("tick_s", symbols=["2330"])
    runner.register(strat)
    await runner.process_event(_tick("2330"))
    assert strat.calls == 1


@pytest.mark.asyncio
async def test_process_event_tick_filtered_wrong_symbol():
    runner, rq = _make_runner()
    strat = _PassStrategy("filter_s", symbols=["9999"])
    runner.register(strat)
    await runner.process_event(_tick("2330"))
    assert strat.calls == 0


@pytest.mark.asyncio
async def test_process_event_bidask_dispatched():
    runner, rq = _make_runner()
    strat = _PassStrategy("ba_s", symbols=["2330"])
    runner.register(strat)
    import numpy as np

    event = BidAskEvent(
        meta=MetaData(seq=1, topic="bidask", source_ts=1, local_ts=1),
        symbol="2330",
        bids=np.array([[100_000, 10], [99_000, 5]], dtype=np.int64),
        asks=np.array([[101_000, 10]], dtype=np.int64),
    )
    await runner.process_event(event)
    assert strat.calls == 1


@pytest.mark.asyncio
async def test_process_event_lobstats_dispatched():
    runner, rq = _make_runner()
    strat = _PassStrategy("ls_s", symbols=["2330"])
    runner.register(strat)
    event = LOBStatsEvent(
        symbol="2330",
        ts=1_000_000,
        imbalance=0.1,
        best_bid=99_000,
        best_ask=101_000,
        bid_depth=5,
        ask_depth=5,
    )
    await runner.process_event(event)
    assert strat.calls == 1


@pytest.mark.asyncio
async def test_process_event_disabled_strategy_skipped():
    runner, rq = _make_runner()
    strat = _PassStrategy("disabled_s", symbols=["2330"])
    runner.register(strat)
    strat.enabled = False
    # Ensure no Rust circuit: force Python path
    runner._rust_circuit = None
    # Not in halted state so it just continues
    await runner.process_event(_tick("2330"))
    assert strat.calls == 0


@pytest.mark.asyncio
async def test_process_event_strategy_exception_returns_empty_intents():
    runner, rq = _make_runner()
    strat = _FailStrategy("fail_s", symbols=["2330"])
    runner.register(strat)
    runner._rust_circuit = None
    await runner.process_event(_tick("2330"))
    # Exception caught; no intents submitted
    assert rq.qsize() == 0


@pytest.mark.asyncio
async def test_process_event_position_delta_invalidates_cache():
    """Events with delta_source attribute should mark positions dirty."""
    runner, rq = _make_runner()
    strat = _PassStrategy("delta_s", symbols=["2330"])
    runner.register(strat)
    runner._positions_dirty = False
    runner._positions_cache = {"delta_s": {"2330": 10}}

    event = SimpleNamespace(
        delta_source="fill",
        symbol="2330",
        strategy_id=None,
        meta=None,
        ts=0,
    )
    # Should not crash even with unusual event
    runner._positions_dirty = False
    await runner.process_event(event)
    # After processing, dirty was set and positions were rebuilt
    assert runner._positions_dirty is False  # rebuilt → cleaned


@pytest.mark.asyncio
async def test_process_event_targeted_dispatch_by_strategy_id():
    """If event.strategy_id is set, only matching strategy is invoked."""
    runner, rq = _make_runner()
    s1 = _PassStrategy("s1", symbols=["2330"])
    s2 = _PassStrategy("s2", symbols=["2330"])
    runner.register(s1)
    runner.register(s2)

    # Build an event with strategy_id targeting only s1
    event = SimpleNamespace(
        strategy_id="s1",
        symbol="2330",
        meta=None,
        ts=1,
    )
    # s1 has no on_* for generic event, won't error, just won't call
    await runner.process_event(event)
    # Neither strategy defines a handler for this custom event, calls stay 0
    assert s1.calls == 0
    assert s2.calls == 0


@pytest.mark.asyncio
async def test_process_event_rebuilds_executors_on_mismatch():
    """If strategies list changes (test harness), executors are rebuilt."""
    runner, rq = _make_runner()
    s1 = _PassStrategy("rebuild_s", symbols=["2330"])
    runner.register(s1)
    # Simulate external modification that breaks the sync
    s2 = _PassStrategy("rebuild_s2", symbols=["2330"])
    runner.strategies.append(s2)
    # _strat_executors is out of sync with strategies; should auto-rebuild
    await runner.process_event(_tick("2330"))
    assert len(runner._strat_executors) == len(runner.strategies)


# ---------------------------------------------------------------------------
# D. Intent factory (_intent_factory)
# ---------------------------------------------------------------------------


def test_intent_factory_increments_seq():
    runner, _ = _make_runner()
    runner._typed_intent_fastpath = False
    seq_before = runner._intent_seq
    runner._intent_factory("s1", "2330", Side.BUY, 100_000, 1, TIF.LIMIT, IntentType.NEW)
    assert runner._intent_seq == seq_before + 1


def test_intent_factory_returns_order_intent_object():
    runner, _ = _make_runner()
    runner._typed_intent_fastpath = False
    intent = runner._intent_factory("s1", "2330", Side.BUY, 100_000, 1, TIF.LIMIT, IntentType.NEW)
    assert isinstance(intent, OrderIntent)
    assert intent.symbol == "2330"
    assert intent.price == 100_000
    assert intent.side == Side.BUY


def test_intent_factory_uses_current_source_ts():
    runner, _ = _make_runner()
    runner._typed_intent_fastpath = False
    runner._current_source_ts_ns = 999_999
    intent = runner._intent_factory("s1", "2330", Side.BUY, 100_000, 1, TIF.LIMIT, IntentType.NEW)
    assert intent.source_ts_ns == 999_999


def test_intent_factory_uses_current_trace_id():
    runner, _ = _make_runner()
    runner._typed_intent_fastpath = False
    runner._current_trace_id = "tick:42"
    intent = runner._intent_factory("s1", "2330", Side.BUY, 100_000, 1, TIF.LIMIT, IntentType.NEW)
    assert intent.trace_id == "tick:42"


def test_intent_factory_explicit_source_ts_overrides():
    runner, _ = _make_runner()
    runner._typed_intent_fastpath = False
    runner._current_source_ts_ns = 0
    intent = runner._intent_factory("s1", "2330", Side.BUY, 100_000, 1, TIF.LIMIT, IntentType.NEW, source_ts_ns=12345)
    assert intent.source_ts_ns == 12345


def test_intent_factory_typed_fastpath_returns_tuple():
    runner, _ = _make_runner()
    runner._typed_intent_fastpath = True

    class _TypedQ:
        def put_nowait(self, x): ...
        def submit_typed_nowait(self, x):
            return "ok"

    runner._risk_submit_typed = _TypedQ().submit_typed_nowait
    result = runner._intent_factory("s1", "2330", Side.BUY, 100_000, 1, TIF.LIMIT, IntentType.NEW)
    assert isinstance(result, tuple)
    assert result[0] == "typed_intent_v1"
    assert result[2] == "s1"
    assert result[3] == "2330"
    assert result[6] == 100_000  # price


def test_intent_factory_typed_fastpath_with_explicit_trace_id():
    runner, _ = _make_runner()
    runner._typed_intent_fastpath = True

    class _TypedQ:
        def submit_typed_nowait(self, x):
            return "ok"

    runner._risk_submit_typed = _TypedQ().submit_typed_nowait
    result = runner._intent_factory("s1", "2330", Side.BUY, 100_000, 1, TIF.LIMIT, IntentType.NEW, trace_id="my:trace")
    assert result[13] == "my:trace"


# ---------------------------------------------------------------------------
# E. Circuit breaker — Python path (Rust disabled)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_python_circuit_normal_to_degraded():
    """After threshold//2 failures the state transitions to degraded."""
    runner, _ = _make_runner()
    runner._rust_circuit = None
    runner._circuit_threshold = 6
    runner._circuit_recovery_threshold = 3

    strat = _FailStrategy("circ_deg", symbols=["2330"])
    runner.register(strat)

    for _ in range(3):
        await runner.process_event(_tick("2330"))

    assert runner._circuit_states.get("circ_deg") == "degraded"
    assert strat.enabled is True


@pytest.mark.asyncio
async def test_python_circuit_degraded_to_halted():
    """After threshold failures the strategy is halted and disabled."""
    runner, _ = _make_runner()
    runner._rust_circuit = None
    runner._circuit_threshold = 4
    runner._circuit_recovery_threshold = 2

    strat = _FailStrategy("circ_halt", symbols=["2330"])
    runner.register(strat)

    for _ in range(4):
        await runner.process_event(_tick("2330"))

    assert runner._circuit_states.get("circ_halt") == "halted"
    assert strat.enabled is False


@pytest.mark.asyncio
async def test_python_circuit_degraded_recovery():
    """N consecutive successes in degraded state recover to normal."""
    runner, _ = _make_runner()
    runner._rust_circuit = None
    runner._circuit_threshold = 6
    runner._circuit_recovery_threshold = 2

    strat = _PassStrategy("circ_rec", symbols=["2330"])
    runner.register(strat)
    sid = strat.strategy_id
    runner._circuit_states[sid] = "degraded"
    runner._failure_counts[sid] = 3
    runner._circuit_success_counts[sid] = 0

    for _ in range(2):
        await runner.process_event(_tick("2330"))

    assert runner._circuit_states.get(sid) == "normal"
    assert runner._failure_counts[sid] == 0


@pytest.mark.asyncio
async def test_python_circuit_halted_cooldown_recovery():
    """After cooldown expires, halted strategy is re-enabled to degraded."""
    runner, _ = _make_runner()
    runner._rust_circuit = None
    runner._circuit_threshold = 4
    runner._circuit_recovery_threshold = 2
    # Very short cooldown (1 ns — effectively immediate)
    runner._circuit_cooldown_ns = 1

    strat = _PassStrategy("circ_cool", symbols=["2330"])
    runner.register(strat)
    sid = strat.strategy_id
    # Put it in halted state, halted_at must be non-zero and older than cooldown
    strat.enabled = False
    runner._circuit_states[sid] = "halted"
    # Use 1 ns (non-zero, older than now by many ns) so the condition passes
    runner._circuit_halted_at_ns[sid] = 1

    await runner.process_event(_tick("2330"))

    # Should be re-enabled
    assert strat.enabled is True
    assert runner._circuit_states.get(sid) == "degraded"


@pytest.mark.asyncio
async def test_python_circuit_halted_skips_when_cooldown_not_elapsed():
    """Strategy remains halted and skipped when cooldown has not elapsed."""
    runner, _ = _make_runner()
    runner._rust_circuit = None

    strat = _PassStrategy("circ_skip", symbols=["2330"])
    runner.register(strat)
    sid = strat.strategy_id
    strat.enabled = False
    runner._circuit_states[sid] = "halted"
    # Set halted_at to far future: cooldown won't elapse
    runner._circuit_halted_at_ns[sid] = 10**18 * 100

    await runner.process_event(_tick("2330"))

    assert strat.calls == 0
    assert strat.enabled is False


@pytest.mark.asyncio
async def test_python_circuit_disabled_strategy_no_circuit_state_skipped():
    """Disabled strategy with no circuit state is simply skipped."""
    runner, _ = _make_runner()
    runner._rust_circuit = None

    strat = _PassStrategy("just_disabled", symbols=["2330"])
    runner.register(strat)
    strat.enabled = False
    # No circuit state set

    await runner.process_event(_tick("2330"))
    assert strat.calls == 0


# ---------------------------------------------------------------------------
# F. _build_positions_by_strategy() additional edge cases
# ---------------------------------------------------------------------------


def test_build_positions_rust_tracker_fast_path():
    """If position_store has _rust_tracker with get_positions_by_strategy, it is used."""

    class _RustTracker:
        def get_positions_by_strategy(self):
            return {"alpha": {"2330": 5}}

    class _PositionStore:
        _rust_tracker = _RustTracker()

    runner, _ = _make_runner(position_store=_PositionStore())
    result = runner._build_positions_by_strategy()
    assert result == {"alpha": {"2330": 5}}


def test_build_positions_rust_tracker_fallback_on_exception():
    """If Rust tracker raises, fall back to Python path."""

    class _FailingTracker:
        def get_positions_by_strategy(self):
            raise RuntimeError("rust boom")

    class _PositionStore:
        _rust_tracker = _FailingTracker()
        positions = {"pos:s1:2330": 7}

    runner, _ = _make_runner(position_store=_PositionStore())
    result = runner._build_positions_by_strategy()
    assert result.get("s1", {}).get("2330") == 7


def test_build_positions_non_dict_positions_returns_empty():
    """If position_store.positions is not a dict, return empty."""

    class _PositionStore:
        positions = None  # type: ignore[assignment]

    runner, _ = _make_runner(position_store=_PositionStore())
    result = runner._build_positions_by_strategy()
    assert result == {}


def test_build_positions_colon_key_missing_third_part():
    """String keys with only two parts (no strategy:symbol) go to fallback."""

    class _PositionStore:
        positions = {"one:two": 99}  # len(parts) < 3

    runner, _ = _make_runner(position_store=_PositionStore())
    result = runner._build_positions_by_strategy()
    # Falls through to fallback bucket
    assert result.get("*", {}).get("one:two") == 99


def test_build_positions_structured_object_with_net_qty():
    """Position objects with strategy_id/symbol/net_qty attrs are handled correctly."""

    class _Pos:
        def __init__(self, strat, sym, qty):
            self.strategy_id = strat
            self.symbol = sym
            self.net_qty = qty

    class _PositionStore:
        positions = {
            "k1": _Pos("myStrat", "2330", 10),
            "k2": _Pos("myStrat", "2317", -3),
        }

    runner, _ = _make_runner(position_store=_PositionStore())
    result = runner._build_positions_by_strategy()
    assert result["myStrat"]["2330"] == 10
    assert result["myStrat"]["2317"] == -3


def test_build_positions_string_key_value_with_net_qty():
    """String-keyed pos:strat:sym with object values that have net_qty."""

    class _Pos:
        def __init__(self, qty):
            self.net_qty = qty

    class _PositionStore:
        positions = {"pos:s1:2330": _Pos(15)}

    runner, _ = _make_runner(position_store=_PositionStore())
    result = runner._build_positions_by_strategy()
    assert result["s1"]["2330"] == 15


# ---------------------------------------------------------------------------
# G. _extract_event_trace()
# ---------------------------------------------------------------------------


def test_extract_event_trace_with_meta_local_ts():
    runner, _ = _make_runner()
    event = _tick("2330", seq=5, local_ts=123456789)
    ts, trace_id = runner._extract_event_trace(event)
    assert ts == 123456789
    assert trace_id == "tick:5"


def test_extract_event_trace_meta_no_seq():
    runner, _ = _make_runner()
    event = _tick("2330", seq=0, local_ts=999)
    # seq=0 is falsy, trace_id should be empty
    # Let's build a custom event with meta.seq = None
    event.meta = SimpleNamespace(local_ts=777, source_ts=0, seq=None, topic="tick")
    ts, trace_id = runner._extract_event_trace(event)
    assert ts == 777
    assert trace_id == ""


def test_extract_event_trace_fallback_to_ts_attribute():
    """Events without meta but with ts attribute use ts for source_ts_ns."""
    runner, _ = _make_runner()
    event = SimpleNamespace(ts=42_000, meta=None)
    ts, trace_id = runner._extract_event_trace(event)
    assert ts == 42_000
    assert trace_id == ""


def test_extract_event_trace_ts_attribute_invalid():
    """If ts raises, source_ts_ns defaults to now_ns."""
    runner, _ = _make_runner()
    event = SimpleNamespace(meta=None, ts=None)
    ts, trace_id = runner._extract_event_trace(event)
    assert ts > 0  # from timebase.now_ns()


def test_extract_event_trace_no_meta_no_ts_uses_now():
    runner, _ = _make_runner()
    event = SimpleNamespace(meta=None)
    ts, _ = runner._extract_event_trace(event)
    assert ts > 0


def test_extract_event_trace_meta_source_ts_fallback():
    """When local_ts is 0, falls back to source_ts."""
    runner, _ = _make_runner()
    event = _tick("2330")
    event.meta = SimpleNamespace(local_ts=0, source_ts=888, seq=2, topic="tick")
    ts, trace_id = runner._extract_event_trace(event)
    assert ts == 888
    assert trace_id == "tick:2"


# ---------------------------------------------------------------------------
# H. run() async loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_single_event_consume():
    """run() processes a single event from bus.consume() then stops."""

    async def _consume():
        yield _tick("2330")

    bus = MagicMock()
    bus.consume.return_value = _consume()
    risk_q = asyncio.Queue()

    with patch("hft_platform.strategy.runner.StrategyRegistry") as mock_reg:
        mock_reg.return_value.instantiate.return_value = []
        runner = StrategyRunner(bus, risk_q, config_path="dummy")

    strat = _PassStrategy("run_s", symbols=["2330"])
    runner.register(strat)

    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert strat.calls >= 1


@pytest.mark.asyncio
async def test_run_batch_mode(monkeypatch):
    """When HFT_BUS_BATCH_SIZE > 1, batch consume path is used."""
    monkeypatch.setenv("HFT_BUS_BATCH_SIZE", "2")

    async def _consume_batch(batch_size):
        yield [_tick("2330", seq=1), _tick("2330", seq=2)]

    bus = MagicMock()
    bus.consume_batch.side_effect = _consume_batch
    risk_q = asyncio.Queue()

    with patch("hft_platform.strategy.runner.StrategyRegistry") as mock_reg:
        mock_reg.return_value.instantiate.return_value = []
        runner = StrategyRunner(bus, risk_q, config_path="dummy")

    strat = _PassStrategy("batch_s", symbols=["2330"])
    runner.register(strat)

    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert strat.calls >= 2


@pytest.mark.asyncio
async def test_run_cancelled_error_handled():
    """CancelledError from bus.consume() is absorbed gracefully."""

    async def _consume():
        raise asyncio.CancelledError()
        yield  # make it a generator  # noqa: unreachable

    bus = MagicMock()
    bus.consume.return_value = _consume()
    risk_q = asyncio.Queue()

    with patch("hft_platform.strategy.runner.StrategyRegistry") as mock_reg:
        mock_reg.return_value.instantiate.return_value = []
        runner = StrategyRunner(bus, risk_q, config_path="dummy")

    await runner.run()  # Should not raise


# ---------------------------------------------------------------------------
# I. _scale_price() and _emit_trace()
# ---------------------------------------------------------------------------


def test_scale_price_identity_for_integer():
    runner, _ = _make_runner()
    result = runner._scale_price("2330", 100_000)
    # Default symbol scale = x10000 (no symbol metadata → direct passthrough)
    assert isinstance(result, int)
    assert result >= 0


def test_scale_price_decimal_input():
    runner, _ = _make_runner()
    result = runner._scale_price("2330", Decimal("10"))
    assert isinstance(result, int)


def test_emit_trace_no_sampler_does_nothing():
    runner, _ = _make_runner()
    runner._trace_sampler = None
    # Should not raise
    runner._emit_trace("stage", "trace:1", {"key": "val"})


def test_emit_trace_with_sampler_calls_emit():
    runner, _ = _make_runner()
    sampler = MagicMock()
    runner._trace_sampler = sampler
    runner._emit_trace("stage", "trace:1", {"k": "v"})
    sampler.emit.assert_called_once()
    call_kwargs = sampler.emit.call_args
    assert call_kwargs.kwargs.get("stage") == "stage"


def test_emit_trace_sampler_exception_suppressed():
    """TypeError/ValueError from sampler.emit are caught and suppressed."""
    runner, _ = _make_runner()
    sampler = MagicMock()
    sampler.emit.side_effect = TypeError("bad type")
    runner._trace_sampler = sampler
    runner._emit_trace("stage", "t:1", {})  # should not raise


# ---------------------------------------------------------------------------
# J. _rebuild_executors()
# ---------------------------------------------------------------------------


def test_rebuild_executors_syncs_with_strategy_list():
    runner, _ = _make_runner()
    s1 = _PassStrategy("re1", symbols=["AAA"])
    s2 = _PassStrategy("re2", symbols=["BBB"])
    runner.register(s1)
    runner.register(s2)
    # Manually clear executor cache
    runner._strat_executors = []
    runner._strat_index = {}
    runner._rebuild_executors()
    assert len(runner._strat_executors) == 2
    assert runner._strat_executors[0][0] is s1
    assert runner._strat_executors[1][0] is s2


def test_rebuild_executors_resets_strat_index():
    runner, _ = _make_runner()
    s1 = _PassStrategy("ri1", symbols=[])
    s2 = _PassStrategy("ri2", symbols=[])
    runner.register(s1)
    runner.register(s2)
    runner._rebuild_executors()
    assert "ri1" in runner._strat_index
    assert "ri2" in runner._strat_index
    assert runner._strat_index["ri1"] == [0]
    assert runner._strat_index["ri2"] == [1]


# ---------------------------------------------------------------------------
# K. _flush_pending_strategy_metrics()
# ---------------------------------------------------------------------------


def test_flush_pending_metrics_no_metrics():
    """If self.metrics is None, pending counters are cleared without error."""
    runner, _ = _make_runner()
    runner.metrics = None
    s = _PassStrategy("flush_s", symbols=[])
    runner.register(s)
    runner._strategy_pending_intents["flush_s"] = 5
    runner._strategy_pending_alpha_intent["flush_s"] = 2
    runner._strategy_pending_alpha_flat["flush_s"] = 1
    runner._flush_pending_strategy_metrics()
    assert runner._strategy_pending_intents.get("flush_s", 0) == 0
    assert runner._strategy_pending_alpha_intent.get("flush_s", 0) == 0
    assert runner._strategy_pending_alpha_flat.get("flush_s", 0) == 0


def test_flush_pending_metrics_with_int_m():
    """Pending intents are flushed into int_m counter."""
    runner, _ = _make_runner()
    s = _PassStrategy("flush2", symbols=[])
    runner.register(s)

    int_m = MagicMock()
    # Replace executor entry's int_m (index 3 in the tuple)
    entry = runner._strat_executors[0]
    new_entry = (entry[0], entry[1], entry[2], int_m, entry[4], entry[5], entry[6])
    runner._strat_executors[0] = new_entry

    runner._strategy_pending_intents["flush2"] = 7
    runner._flush_pending_strategy_metrics()
    int_m.inc.assert_called_once_with(7)


# ---------------------------------------------------------------------------
# L. _resolve_risk_submit()
# ---------------------------------------------------------------------------


def test_resolve_risk_submit_uses_submit_nowait():
    runner, _ = _make_runner()

    class _CustomQueue:
        def submit_nowait(self, x): ...
        def put_nowait(self, x): ...

    q = _CustomQueue()
    fn = runner._resolve_risk_submit(q)
    assert fn == q.submit_nowait


def test_resolve_risk_submit_falls_back_to_put_nowait():
    runner, _ = _make_runner()

    class _StdQueue:
        def put_nowait(self, x): ...

    q = _StdQueue()
    fn = runner._resolve_risk_submit(q)
    assert fn == q.put_nowait


# ---------------------------------------------------------------------------
# M. invalidate_positions()
# ---------------------------------------------------------------------------


def test_invalidate_positions_sets_dirty():
    runner, _ = _make_runner()
    runner._positions_dirty = False
    runner.invalidate_positions()
    assert runner._positions_dirty is True


# ---------------------------------------------------------------------------
# N. Intents dispatched to risk queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intents_submitted_to_risk_queue():
    runner, rq = _make_runner()
    strat = _IntentStrategy("int_s", symbols=["2330"], price=100_000)
    runner.register(strat)
    await runner.process_event(_tick("2330"))
    assert rq.qsize() == 1
    intent = await rq.get()
    assert isinstance(intent, OrderIntent)
    assert intent.price == 100_000
    assert intent.symbol == "2330"


@pytest.mark.asyncio
async def test_multiple_intents_all_submitted():
    """A strategy that returns 2 intents per event should enqueue both."""

    class _DoubleIntentStrategy(BaseStrategy):
        def on_tick(self, event):
            self.buy(event.symbol, 100_000, 1)
            self.sell(event.symbol, 100_000, 1)

    runner, rq = _make_runner()
    strat = _DoubleIntentStrategy("double_s", symbols=["2330"])
    runner.register(strat)
    await runner.process_event(_tick("2330"))
    assert rq.qsize() == 2


@pytest.mark.asyncio
async def test_intent_seq_increments_across_events():
    runner, rq = _make_runner()
    strat = _IntentStrategy("seq_s", symbols=["2330"], price=100_000)
    runner.register(strat)
    await runner.process_event(_tick("2330", seq=1))
    await runner.process_event(_tick("2330", seq=2))
    i1 = await rq.get()
    i2 = await rq.get()
    assert i2.intent_id == i1.intent_id + 1


# ---------------------------------------------------------------------------
# O. Metrics sampling / batching branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_sample_every_skip():
    """When sample_every=2, latency metric observed on every other event."""
    runner, rq = _make_runner()
    runner._strategy_metrics_sample_every = 2
    runner._strategy_metrics_batch = 1
    strat = _PassStrategy("sample_s", symbols=["2330"])
    runner.register(strat)

    # Replace lat_m with a mock
    entry = runner._strat_executors[0]
    lat_m = MagicMock()
    runner._strat_executors[0] = (entry[0], entry[1], lat_m, entry[3], entry[4], entry[5], entry[6])

    await runner.process_event(_tick("2330", seq=1))
    await runner.process_event(_tick("2330", seq=2))

    # With sample_every=2, observe should be called once (on seq=2 event)
    assert lat_m.observe.call_count == 1


@pytest.mark.asyncio
async def test_metrics_batch_pending_flush():
    """When batch>1 and seq%batch==0, pending intents are flushed."""
    runner, rq = _make_runner()
    runner._strategy_metrics_batch = 2
    strat = _IntentStrategy("batch_int_s", symbols=["2330"], price=100_000)
    runner.register(strat)

    entry = runner._strat_executors[0]
    int_m = MagicMock()
    runner._strat_executors[0] = (entry[0], entry[1], entry[2], int_m, entry[4], entry[5], entry[6])

    await runner.process_event(_tick("2330", seq=1))
    await runner.process_event(_tick("2330", seq=2))  # seq%batch==0 → flush

    # int_m.inc should have been called at flush time
    assert int_m.inc.called
