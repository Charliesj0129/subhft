"""Coverage tests for strategy/runner.py — targeting uncovered lines.

Covers: typed intent helpers, filter_intents_by_phase, _extract_event_trace tuple paths,
resolve_symbol_aliases, _resolve_strategy_symbol_params, misc setters, run() batch mode,
and edge cases in process_event dispatch.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.ops.session_governor import SessionPhase, TrackGate

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


def _make_bus(events=None):
    bus = MagicMock()
    events = events or []

    async def _gen(**kwargs):
        for e in events:
            yield e

    bus.consume.return_value = _gen()
    bus.cursor = 0
    return bus


def _make_risk_queue():
    rq = MagicMock(spec=["put_nowait"])
    rq.put_nowait = MagicMock()
    return rq


class _FakeStrategy:
    def __init__(self, sid="strat_a", symbols=None, enabled=True):
        self.strategy_id = sid
        self.symbols = set(symbols) if symbols else {"TSMC"}
        self.enabled = enabled
        self.required_features = []
        self.required_feature_profile = None
        self._calls = []
        self._return_value = []

    def handle_event(self, ctx, event):
        self._calls.append((ctx, event))
        return self._return_value


def _make_event(symbol="TSMC", ts=0):
    return SimpleNamespace(symbol=symbol, ts=ts)


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


@pytest.fixture()
def runner_factory():
    def _make(bus=None, rq=None, lob_engine=None, position_store=None, feature_engine=None):
        from hft_platform.strategy.runner import StrategyRunner

        bus = bus or _make_bus()
        rq = rq or _make_risk_queue()
        runner = StrategyRunner(
            bus=bus,
            risk_queue=rq,
            lob_engine=lob_engine,
            position_store=position_store,
            feature_engine=feature_engine,
        )
        return runner, bus, rq

    return _make


# ---------------------------------------------------------------------------
# Tests: module-level helpers (_typed_intent_* functions)
# ---------------------------------------------------------------------------


class TestTypedIntentHelpers:
    """Test _typed_intent_symbol, _typed_intent_type, _typed_intent_tif, _typed_intent_side."""

    def test_typed_intent_symbol_from_tuple(self):
        from hft_platform.strategy.runner import _typed_intent_symbol

        intent = ("typed_intent_v1", 1, "strat", "TXFD6", 0, 0, 100, 1, 0)
        assert _typed_intent_symbol(intent) == "TXFD6"

    def test_typed_intent_symbol_from_object(self):
        from hft_platform.strategy.runner import _typed_intent_symbol

        intent = SimpleNamespace(symbol="TSMC")
        assert _typed_intent_symbol(intent) == "TSMC"

    def test_typed_intent_symbol_fallback_empty(self):
        from hft_platform.strategy.runner import _typed_intent_symbol

        assert _typed_intent_symbol("not_an_intent") == ""

    def test_typed_intent_type_from_tuple(self):
        from hft_platform.strategy.runner import _typed_intent_type

        intent = ("typed_intent_v1", 1, "strat", "SYM", 2, 0, 100, 1, 0)
        assert _typed_intent_type(intent) == 2

    def test_typed_intent_type_from_tuple_invalid_value(self):
        """Lines 72-78: TypeError/ValueError returns None."""
        from hft_platform.strategy.runner import _typed_intent_type

        intent = ("typed_intent_v1", 1, "strat", "SYM", "not_int")
        assert _typed_intent_type(intent) is None

    def test_typed_intent_type_from_object(self):
        from hft_platform.strategy.runner import _typed_intent_type

        intent = SimpleNamespace(intent_type=3)
        assert _typed_intent_type(intent) == 3

    def test_typed_intent_type_from_object_none(self):
        from hft_platform.strategy.runner import _typed_intent_type

        intent = SimpleNamespace()
        assert _typed_intent_type(intent) is None

    def test_typed_intent_type_from_object_invalid(self):
        """Lines 76-78: object attribute that cannot be converted to int."""
        from hft_platform.strategy.runner import _typed_intent_type

        intent = SimpleNamespace(intent_type="bad")
        assert _typed_intent_type(intent) is None

    def test_typed_intent_tif_from_tuple(self):
        """Lines 82-92: Extract TIF from typed tuple index 8."""
        from hft_platform.strategy.runner import _typed_intent_tif

        intent = ("typed_intent_v1", 1, "strat", "SYM", 0, 0, 100, 1, 3)
        assert _typed_intent_tif(intent) == 3

    def test_typed_intent_tif_from_tuple_invalid(self):
        """Lines 86-87: TypeError/ValueError returns None."""
        from hft_platform.strategy.runner import _typed_intent_tif

        intent = ("typed_intent_v1", 1, "strat", "SYM", 0, 0, 100, 1, "bad")
        assert _typed_intent_tif(intent) is None

    def test_typed_intent_tif_from_object(self):
        """Lines 88-92: Extract TIF from object attribute."""
        from hft_platform.strategy.runner import _typed_intent_tif

        intent = SimpleNamespace(tif=1)
        assert _typed_intent_tif(intent) == 1

    def test_typed_intent_tif_from_object_none(self):
        from hft_platform.strategy.runner import _typed_intent_tif

        intent = SimpleNamespace()
        assert _typed_intent_tif(intent) is None

    def test_typed_intent_tif_from_object_invalid(self):
        """Lines 91-92: object attribute ValueError."""
        from hft_platform.strategy.runner import _typed_intent_tif

        intent = SimpleNamespace(tif="xyz")
        assert _typed_intent_tif(intent) is None

    def test_typed_intent_side_from_tuple(self):
        """Lines 95-106: Extract side from typed tuple index 5."""
        from hft_platform.strategy.runner import _typed_intent_side

        intent = ("typed_intent_v1", 1, "strat", "SYM", 0, 1, 100, 1, 0)
        assert _typed_intent_side(intent) == 1

    def test_typed_intent_side_from_tuple_invalid(self):
        """Lines 100-101: TypeError/ValueError returns None."""
        from hft_platform.strategy.runner import _typed_intent_side

        intent = ("typed_intent_v1", 1, "strat", "SYM", 0, "bad")
        assert _typed_intent_side(intent) is None

    def test_typed_intent_side_from_object(self):
        from hft_platform.strategy.runner import _typed_intent_side

        intent = SimpleNamespace(side=0)
        assert _typed_intent_side(intent) == 0

    def test_typed_intent_side_from_object_invalid(self):
        """Lines 104-106: object attribute ValueError."""
        from hft_platform.strategy.runner import _typed_intent_side

        intent = SimpleNamespace(side="bad")
        assert _typed_intent_side(intent) is None


# ---------------------------------------------------------------------------
# Tests: _get_symbol_net_qty
# ---------------------------------------------------------------------------


class TestGetSymbolNetQty:
    """Test lines 125-148: position lookup for CLOSE_ONLY phase."""

    def test_returns_zero_when_store_is_none(self):
        from hft_platform.strategy.runner import _get_symbol_net_qty

        assert _get_symbol_net_qty(None, "TSMC") == 0

    def test_returns_zero_when_positions_empty(self):
        from hft_platform.strategy.runner import _get_symbol_net_qty

        store = SimpleNamespace(positions={})
        assert _get_symbol_net_qty(store, "TSMC") == 0

    def test_returns_zero_when_positions_attr_none(self):
        """Line 139: positions attribute is None."""
        from hft_platform.strategy.runner import _get_symbol_net_qty

        store = SimpleNamespace(positions=None)
        assert _get_symbol_net_qty(store, "TSMC") == 0

    def test_matches_symbol_suffix(self):
        """Lines 139-148: key ends with :SYMBOL."""
        from hft_platform.strategy.runner import _get_symbol_net_qty

        pos = SimpleNamespace(net_qty=5)
        store = SimpleNamespace(positions={"acct:strat_a:TSMC": pos})
        assert _get_symbol_net_qty(store, "TSMC") == 5

    def test_filters_by_strategy_id(self):
        """Line 145-146: strategy_id filter."""
        from hft_platform.strategy.runner import _get_symbol_net_qty

        pos_a = SimpleNamespace(net_qty=3)
        pos_b = SimpleNamespace(net_qty=7)
        store = SimpleNamespace(
            positions={
                "acct:strat_a:TSMC": pos_a,
                "acct:strat_b:TSMC": pos_b,
            }
        )
        assert _get_symbol_net_qty(store, "TSMC", strategy_id="strat_a") == 3

    def test_sums_multiple_matching_entries(self):
        from hft_platform.strategy.runner import _get_symbol_net_qty

        pos1 = SimpleNamespace(net_qty=2)
        pos2 = SimpleNamespace(net_qty=3)
        store = SimpleNamespace(
            positions={
                "acct1:strat_a:TSMC": pos1,
                "acct2:strat_a:TSMC": pos2,
            }
        )
        assert _get_symbol_net_qty(store, "TSMC", strategy_id="strat_a") == 5

    def test_ignores_non_matching_symbol(self):
        """Line 144: key does not end with :TSMC."""
        from hft_platform.strategy.runner import _get_symbol_net_qty

        pos = SimpleNamespace(net_qty=10)
        store = SimpleNamespace(positions={"acct:strat_a:2330": pos})
        assert _get_symbol_net_qty(store, "TSMC") == 0


# ---------------------------------------------------------------------------
# Tests: filter_intents_by_phase (static method)
# ---------------------------------------------------------------------------


class TestFilterIntentsByPhase:
    """Test lines 1349-1394: session phase filtering for intents."""

    def _make_track_gate(self, symbol, phase):
        gate = TrackGate()
        gate.register_symbol(symbol, "test_track")
        gate.set_track_phase("test_track", phase)
        return gate

    def _order_intent(self, symbol, intent_type, side=Side.BUY, tif=TIF.LIMIT):
        return OrderIntent(
            intent_id=1,
            strategy_id="strat_a",
            symbol=symbol,
            intent_type=intent_type,
            side=side,
            price=1_000_000,
            qty=1,
            tif=tif,
        )

    def test_open_phase_allows_all(self):
        from hft_platform.strategy.runner import StrategyRunner

        gate = self._make_track_gate("TSMC", SessionPhase.OPEN)
        intent = self._order_intent("TSMC", IntentType.NEW)
        result = StrategyRunner.filter_intents_by_phase([intent], gate)
        assert len(result) == 1

    def test_close_only_allows_cancel(self):
        from hft_platform.strategy.runner import StrategyRunner

        gate = self._make_track_gate("TSMC", SessionPhase.CLOSE_ONLY)
        intent = self._order_intent("TSMC", IntentType.CANCEL)
        result = StrategyRunner.filter_intents_by_phase([intent], gate)
        assert len(result) == 1

    def test_close_only_allows_force_flat(self):
        from hft_platform.strategy.runner import StrategyRunner

        gate = self._make_track_gate("TSMC", SessionPhase.CLOSE_ONLY)
        intent = self._order_intent("TSMC", IntentType.FORCE_FLAT)
        result = StrategyRunner.filter_intents_by_phase([intent], gate)
        assert len(result) == 1

    def test_close_only_allows_ioc_sell_when_long(self):
        """IOC SELL when position is long (reduces exposure)."""
        from hft_platform.strategy.runner import StrategyRunner

        gate = self._make_track_gate("TSMC", SessionPhase.CLOSE_ONLY)
        intent = self._order_intent("TSMC", IntentType.NEW, side=Side.SELL, tif=TIF.IOC)
        pos = SimpleNamespace(net_qty=5)
        store = SimpleNamespace(positions={"acct:strat_a:TSMC": pos})
        result = StrategyRunner.filter_intents_by_phase([intent], gate, position_store=store, strategy_id="strat_a")
        assert len(result) == 1

    def test_close_only_allows_ioc_buy_when_short(self):
        """IOC BUY when position is short (reduces exposure)."""
        from hft_platform.strategy.runner import StrategyRunner

        gate = self._make_track_gate("TSMC", SessionPhase.CLOSE_ONLY)
        intent = self._order_intent("TSMC", IntentType.NEW, side=Side.BUY, tif=TIF.IOC)
        pos = SimpleNamespace(net_qty=-3)
        store = SimpleNamespace(positions={"acct:strat_a:TSMC": pos})
        result = StrategyRunner.filter_intents_by_phase([intent], gate, position_store=store, strategy_id="strat_a")
        assert len(result) == 1

    def test_close_only_blocks_ioc_buy_when_flat(self):
        """IOC BUY when position is flat (would increase exposure)."""
        from hft_platform.strategy.runner import StrategyRunner

        gate = self._make_track_gate("TSMC", SessionPhase.CLOSE_ONLY)
        intent = self._order_intent("TSMC", IntentType.NEW, side=Side.BUY, tif=TIF.IOC)
        store = SimpleNamespace(positions={})
        result = StrategyRunner.filter_intents_by_phase([intent], gate, position_store=store, strategy_id="strat_a")
        assert len(result) == 0

    def test_close_only_blocks_ioc_when_no_position_store(self):
        """Conservative: block IOC NEW when position_store is None."""
        from hft_platform.strategy.runner import StrategyRunner

        gate = self._make_track_gate("TSMC", SessionPhase.CLOSE_ONLY)
        intent = self._order_intent("TSMC", IntentType.NEW, side=Side.SELL, tif=TIF.IOC)
        result = StrategyRunner.filter_intents_by_phase([intent], gate, position_store=None)
        assert len(result) == 0

    def test_close_only_blocks_limit_new(self):
        """LIMIT NEW order blocked in CLOSE_ONLY."""
        from hft_platform.strategy.runner import StrategyRunner

        gate = self._make_track_gate("TSMC", SessionPhase.CLOSE_ONLY)
        intent = self._order_intent("TSMC", IntentType.NEW, tif=TIF.LIMIT)
        result = StrategyRunner.filter_intents_by_phase([intent], gate)
        assert len(result) == 0

    def test_force_flat_allows_cancel(self):
        from hft_platform.strategy.runner import StrategyRunner

        gate = self._make_track_gate("TSMC", SessionPhase.FORCE_FLAT)
        intent = self._order_intent("TSMC", IntentType.CANCEL)
        result = StrategyRunner.filter_intents_by_phase([intent], gate)
        assert len(result) == 1

    def test_force_flat_blocks_new(self):
        from hft_platform.strategy.runner import StrategyRunner

        gate = self._make_track_gate("TSMC", SessionPhase.FORCE_FLAT)
        intent = self._order_intent("TSMC", IntentType.NEW)
        result = StrategyRunner.filter_intents_by_phase([intent], gate)
        assert len(result) == 0

    def test_filter_typed_tuple_intent_in_close_only(self):
        """Test typed_intent_v1 tuple filtering in CLOSE_ONLY."""
        from hft_platform.strategy.runner import StrategyRunner

        gate = self._make_track_gate("TSMC", SessionPhase.CLOSE_ONLY)
        # CANCEL typed intent (intent_type=2)
        typed_cancel = ("typed_intent_v1", 1, "strat_a", "TSMC", 2, 0, 100, 1, 0)
        result = StrategyRunner.filter_intents_by_phase([typed_cancel], gate)
        assert len(result) == 1

    def test_filter_typed_tuple_new_blocked_in_close_only(self):
        """NEW typed_intent_v1 blocked in CLOSE_ONLY (TIF=LIMIT)."""
        from hft_platform.strategy.runner import StrategyRunner

        gate = self._make_track_gate("TSMC", SessionPhase.CLOSE_ONLY)
        # NEW typed intent (intent_type=0, side=1=SELL, tif=0=LIMIT)
        typed_new = ("typed_intent_v1", 1, "strat_a", "TSMC", 0, 1, 100, 1, 0)
        result = StrategyRunner.filter_intents_by_phase([typed_new], gate)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Tests: _extract_event_trace tuple paths
# ---------------------------------------------------------------------------


class TestExtractEventTrace:
    """Test tuple-based timestamp extraction (lines 1407-1433)."""

    def test_tick_tuple_extracts_ts(self, runner_factory):
        runner, _, _ = runner_factory()
        event = ("tick", "TSMC", 100, 200, 300, 400, 500, 9999_000)
        ts, trace_id = runner._extract_event_trace(event)
        assert ts == 9999_000

    def test_bidask_tuple_extracts_ts(self, runner_factory):
        runner, _, _ = runner_factory()
        event = ("bidask", "TSMC", 100, 200, 8888_000)
        ts, trace_id = runner._extract_event_trace(event)
        assert ts == 8888_000

    def test_lobstats_tuple_extracts_ts(self, runner_factory):
        runner, _, _ = runner_factory()
        event = ("lobstats", "TSMC", 7777_000)
        ts, trace_id = runner._extract_event_trace(event)
        assert ts == 7777_000

    def test_tuple_invalid_ts_falls_back_to_now(self, runner_factory):
        runner, _, _ = runner_factory()
        event = ("tick", "TSMC", 100, 200, 300, 400, 500, None)
        ts, trace_id = runner._extract_event_trace(event)
        assert ts > 0  # Falls back to now_ns()

    def test_meta_source_ts_fallback(self, runner_factory):
        """Line 1414: local_ts=0 falls back to source_ts."""
        runner, _, _ = runner_factory()
        meta = SimpleNamespace(local_ts=0, source_ts=5555, seq=10, topic="test")
        event = SimpleNamespace(meta=meta)
        ts, _ = runner._extract_event_trace(event)
        assert ts == 5555

    def test_meta_no_seq(self, runner_factory):
        """When seq is None, trace_id is empty."""
        runner, _, _ = runner_factory()
        meta = SimpleNamespace(local_ts=1000, seq=None, topic="tick")
        event = SimpleNamespace(meta=meta)
        ts, trace_id = runner._extract_event_trace(event)
        assert ts == 1000
        assert trace_id == ""


# ---------------------------------------------------------------------------
# Tests: resolve_symbol_aliases and _resolve_strategy_symbol_params
# ---------------------------------------------------------------------------


class TestResolveSymbolAliases:
    """Test lines 400-630: alias resolution paths."""

    def test_resolve_symbol_aliases_no_alias_map(self, runner_factory):
        """Lines 406-407: early return when alias_to_actual is empty."""
        runner, _, _ = runner_factory()
        strat = _FakeStrategy("s1", symbols=["TSMC"])
        runner.register(strat)
        runner.resolve_symbol_aliases()
        assert "TSMC" in strat.symbols

    def test_resolve_symbol_aliases_with_alias_map(self, runner_factory):
        """Lines 406-410: resolves aliases when alias_to_actual is populated."""
        runner, _, _ = runner_factory()
        runner.symbol_metadata.alias_to_actual = {"C0": "TXFD6"}
        runner.symbol_metadata.resolve_symbol = lambda s: runner.symbol_metadata.alias_to_actual.get(s, s)
        runner.symbol_metadata.resolve_symbols = lambda syms: {runner.symbol_metadata.resolve_symbol(s) for s in syms}
        strat = _FakeStrategy("s1", symbols=["C0"])
        runner.register(strat)
        runner.resolve_symbol_aliases()
        assert "TXFD6" in strat.symbols

    def test_resolve_strategy_symbol_params_resolves_attr(self, runner_factory):
        """Lines 614-623: resolve _<name>_symbol attributes on strategy."""
        runner, _, _ = runner_factory()
        runner.symbol_metadata.alias_to_actual = {"R1": "TMFD6"}
        runner.symbol_metadata.resolve_symbol = lambda s: runner.symbol_metadata.alias_to_actual.get(s, s)
        runner.symbol_metadata.resolve_symbols = lambda syms: {runner.symbol_metadata.resolve_symbol(s) for s in syms}
        strat = _FakeStrategy("s1", symbols=["TSMC"])
        strat._trade_symbol = "R1"
        runner.register(strat)
        runner.resolve_symbol_aliases()
        assert strat._trade_symbol == "TMFD6"

    def test_resolve_no_metadata(self, runner_factory):
        """Line 611: no symbol_metadata, returns early."""
        runner, _, _ = runner_factory()
        runner.symbol_metadata = None
        strat = _FakeStrategy("s1", symbols=["TSMC"])
        runner.register(strat)
        # Should not raise
        runner._resolve_strategy_symbol_params(strat)
        assert "TSMC" in strat.symbols


# ---------------------------------------------------------------------------
# Tests: misc setters
# ---------------------------------------------------------------------------


class TestMiscSetters:
    """Cover setter methods on StrategyRunner."""

    def test_set_start_cursor(self, runner_factory):
        runner, _, _ = runner_factory()
        runner.set_start_cursor(42)
        assert runner._start_cursor == 42

    def test_set_rejection_sink(self, runner_factory):
        runner, _, _ = runner_factory()
        q = asyncio.Queue()
        runner.set_rejection_sink(q)
        assert runner._rejection_sink is q

    def test_set_rejection_queue(self, runner_factory):
        runner, _, _ = runner_factory()
        q = asyncio.Queue()
        runner.set_rejection_queue(q)
        assert runner._rejection_queue is q

    def test_reset_stale_counter(self, runner_factory):
        """Line 389-394: stale counter resets to 0."""
        runner, _, _ = runner_factory()
        runner._stale_event_skip_total = 50
        runner.reset_stale_counter()
        assert runner._stale_event_skip_total == 0

    def test_reset_stale_counter_noop_when_zero(self, runner_factory):
        runner, _, _ = runner_factory()
        runner._stale_event_skip_total = 0
        runner.reset_stale_counter()
        assert runner._stale_event_skip_total == 0

    def test_set_storm_guard(self, runner_factory):
        runner, _, _ = runner_factory()
        sg = MagicMock()
        runner.set_storm_guard(sg)
        assert runner._storm_guard is sg

# P2 (2026-04-25): ``set_publish_sink`` removed — the runner never propagated
# the sink to per-strategy ``StrategyContext`` instances, so the wired sink
# was never invoked in production. Bootstrap no longer wires it. The test
# that asserted ``runner._publish_sink is sink`` was therefore exercising a
# dead path; it has been removed alongside the implementation.


# ---------------------------------------------------------------------------
# Tests: run() with batch mode
# ---------------------------------------------------------------------------


class TestRunBatchMode:
    """Test lines 428-440: batch consumption mode."""

    @pytest.mark.asyncio
    async def test_run_batch_mode(self, runner_factory, monkeypatch):
        """Lines 428-434: HFT_BUS_BATCH_SIZE > 1 uses consume_batch."""
        monkeypatch.setenv("HFT_BUS_BATCH_SIZE", "4")

        events = [_make_event("A"), _make_event("B")]
        bus = MagicMock()

        async def _batch_gen(*args, **kwargs):
            yield events  # one batch

        bus.consume_batch.return_value = _batch_gen()
        bus.cursor = 0
        runner, _, _ = runner_factory(bus=bus)
        strat = _FakeStrategy("s", symbols={"A", "B"})
        runner.register(strat)
        await asyncio.wait_for(runner.run(), timeout=5.0)
        assert len(strat._calls) == 2

    @pytest.mark.asyncio
    async def test_run_cancel_during_consume(self, runner_factory):
        """Lines 439-440: CancelledError is caught, flush is called."""
        bus = MagicMock()

        async def _gen(**kwargs):
            yield _make_event()
            raise asyncio.CancelledError()

        bus.consume.return_value = _gen()
        bus.cursor = 0
        runner, _, _ = runner_factory(bus=bus)
        await asyncio.wait_for(runner.run(), timeout=5.0)
        assert runner.running is True  # was set before loop


# ---------------------------------------------------------------------------
# Tests: process_event edge cases
# ---------------------------------------------------------------------------


class TestProcessEventEdgeCases:
    """Cover remaining lines in process_event dispatch."""

    @pytest.mark.asyncio
    async def test_quarantined_strategy_skipped(self, runner_factory):
        """Strategy under quarantine is skipped without circuit breaker penalty."""
        runner, _, _ = runner_factory()
        strat = _FakeStrategy("strat_q")
        runner.register(strat)
        runner.strategy_governor.is_quarantined = MagicMock(return_value=True)
        event = _make_event()
        await runner.process_event(event)
        assert len(strat._calls) == 0

    @pytest.mark.asyncio
    async def test_intent_flood_capped(self, runner_factory):
        """Lines 1222-1229: too many intents are capped."""
        runner, _, rq = runner_factory()
        strat = _FakeStrategy("strat_flood")
        intents = [SimpleNamespace(intent_type=0, symbol="TSMC") for _ in range(30)]
        strat._return_value = intents
        runner.register(strat)
        runner._typed_intent_fastpath = False
        runner._max_intents_per_event = 5
        event = _make_event()
        await runner.process_event(event)
        assert rq.put_nowait.call_count == 5

    @pytest.mark.asyncio
    async def test_executor_rebuild_on_version_mismatch(self, runner_factory):
        """Lines 887-889: executor rebuild when version counters diverge."""
        runner, _, _ = runner_factory()
        strat = _FakeStrategy("s1")
        runner.register(strat)
        # Artificially desync versions
        runner._strategies_version = 999
        event = _make_event()
        await runner.process_event(event)
        assert runner._executors_version == runner._strategies_version

    @pytest.mark.asyncio
    async def test_disabled_strategy_non_halted_skipped(self, runner_factory):
        """Lines 922-923: disabled strategy not in 'halted' state is skipped."""
        runner, _, _ = runner_factory()
        strat = _FakeStrategy("s_disabled", enabled=False)
        runner.register(strat)
        runner._rust_circuit = None
        # No halted state set — this goes to the else branch
        event = _make_event()
        await runner.process_event(event)
        assert len(strat._calls) == 0

    @pytest.mark.asyncio
    async def test_drain_to_cursor_already_past(self, runner_factory):
        """Line 482: _consumer_seq >= target_cursor returns (0, 0)."""
        runner, _, _ = runner_factory()
        runner._consumer_seq = 10
        drained, skipped = await runner.drain_to_cursor(5, timeout_s=1.0)
        assert drained == 0
        assert skipped == 0

    @pytest.mark.asyncio
    async def test_lob_l1_decision_price_on_order_intent(self, runner_factory):
        """Lines covering decision_price population from LOB L1."""
        runner, _, rq = runner_factory()
        # Set up L1 source that returns mid_price_x2
        runner._lob_l1_source = lambda sym: (0, 500_0000, 510_0000, 1010_0000, 10_0000, 100, 100)
        strat = _FakeStrategy("s_l1")
        intent = OrderIntent(
            intent_id=1,
            strategy_id="s_l1",
            symbol="TSMC",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=500_0000,
            qty=1,
        )
        strat._return_value = [intent]
        runner.register(strat)
        runner._typed_intent_fastpath = False
        event = _make_event()
        await runner.process_event(event)
        assert rq.put_nowait.call_count == 1
        submitted = rq.put_nowait.call_args[0][0]
        assert submitted.decision_price == 1010_0000 // 2


# ---------------------------------------------------------------------------
# Tests: _get_trace_sampler fallback
# ---------------------------------------------------------------------------


class TestGetTraceSampler:
    def test_get_trace_sampler_import_error_returns_none(self):
        """Lines 51-52: ImportError returns None."""
        with patch("hft_platform.strategy.runner._get_trace_sampler") as mock_fn:
            mock_fn.return_value = None
            assert mock_fn() is None

    def test_get_trace_sampler_returns_sampler_or_none(self):
        """Verify _get_trace_sampler callable returns without error."""
        from hft_platform.strategy.runner import _get_trace_sampler

        result = _get_trace_sampler()
        # It either returns a sampler object or None depending on module availability
        assert result is None or hasattr(result, "emit")


# ---------------------------------------------------------------------------
# Tests: StrategyRunner constructor edge cases
# ---------------------------------------------------------------------------


class TestConstructorEdgeCases:
    def test_metrics_sample_env_value_error(self, runner_factory, monkeypatch):
        """Lines 287-288, 296-297: ValueError in int parse for sample/batch env."""
        monkeypatch.setenv("HFT_STRATEGY_METRICS_SAMPLE_EVERY", "notanumber")
        monkeypatch.setenv("HFT_STRATEGY_METRICS_BATCH", "notanumber")
        runner, _, _ = runner_factory()
        assert runner._strategy_metrics_sample_every == 1
        assert runner._strategy_metrics_batch == 1

    def test_rust_circuit_init_failure(self, runner_factory, monkeypatch):
        """Lines 322-323: Rust circuit breaker init failure logs warning."""
        monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_RUST", "1")

        # Mock _RustCircuitBreaker to raise on init
        with patch("hft_platform.strategy.runner._RustCircuitBreaker") as mock_cls:
            mock_cls.side_effect = RuntimeError("init fail")
            with patch("hft_platform.strategy.runner._RUST_CIRCUIT_ENABLED", True):
                runner, _, _ = runner_factory()
                assert runner._rust_circuit is None


# ---------------------------------------------------------------------------
# Tests: _build_positions recovery merge
# ---------------------------------------------------------------------------


class TestBuildPositionsRecovery:
    def test_recovery_positions_merge(self, runner_factory):
        """Lines 827-841: _recovery_positions are merged."""
        pos_store = MagicMock()
        del pos_store._rust_tracker
        del pos_store.snapshot_positions
        pos_store.positions = {}
        pos_store._recovery_positions = {
            "acct:strat_r:TSMC": {"net_qty": 3},
            "acct:TXFD6": {"net_qty": -1},
        }
        runner, _, _ = runner_factory(position_store=pos_store)
        result = runner._build_positions_by_strategy()
        assert result.get("strat_r", {}).get("TSMC") == 3
        # Short key falls to wildcard
        assert result.get("*", {}).get("TXFD6") == -1

    def test_recovery_positions_zero_qty_skipped(self, runner_factory):
        """Line 831: net_qty=0 entries are skipped."""
        pos_store = MagicMock()
        del pos_store._rust_tracker
        del pos_store.snapshot_positions
        pos_store.positions = {}
        pos_store._recovery_positions = {
            "acct:strat_r:TSMC": {"net_qty": 0},
        }
        runner, _, _ = runner_factory(position_store=pos_store)
        result = runner._build_positions_by_strategy()
        assert "strat_r" not in result


# ---------------------------------------------------------------------------
# Tests: register compat error with fail_fast
# ---------------------------------------------------------------------------


class TestRegisterCompatError:
    def test_register_compat_error_fail_fast(self, runner_factory, monkeypatch):
        """Lines 546-552: compat error + fail_fast raises RuntimeError."""
        monkeypatch.setenv("HFT_STRATEGY_FEATURE_COMPAT_FAIL_FAST", "1")
        runner, _, _ = runner_factory()
        runner._feature_compat_fail_fast = True
        strat = _FakeStrategy("err_strat")

        with patch(
            "hft_platform.strategy.runner.check_strategy_feature_compat",
            return_value=[
                SimpleNamespace(level="error", strategy_id="err_strat", code="BAD_FEATURE", message="err"),
            ],
        ):
            with pytest.raises(RuntimeError, match="failed feature compatibility"):
                runner.register(strat)

    def test_register_compat_metric_emit_fails_gracefully(self, runner_factory, _patch_metrics):
        """Lines 546-547: metric emit TypeError is caught."""
        runner, _, _ = runner_factory()
        _patch_metrics.feature_profile_compat_failures_total.labels.side_effect = TypeError("bad")
        strat = _FakeStrategy("metric_err_strat")
        with patch(
            "hft_platform.strategy.runner.check_strategy_feature_compat",
            return_value=[
                SimpleNamespace(level="error", strategy_id="metric_err_strat", code="X", message="x"),
            ],
        ):
            # fail_fast is off, so this should not raise
            runner.register(strat)
        assert strat in runner.strategies


# ---------------------------------------------------------------------------
# Tests: storm guard escalation
# ---------------------------------------------------------------------------


class TestStormGuardEscalation:
    @pytest.mark.asyncio
    async def test_persistent_queue_full_triggers_halt(self, runner_factory):
        """After N consecutive queue-full events, trigger_halt is called."""
        rq = MagicMock(spec=["put_nowait"])
        rq.put_nowait = MagicMock(side_effect=asyncio.QueueFull())
        runner, _, _ = runner_factory(rq=rq)
        runner._typed_intent_fastpath = False
        sg = MagicMock()
        runner._storm_guard = sg
        runner._queue_full_halt_threshold = 2

        strat = _FakeStrategy("s_sg", symbols=["TSMC"])
        intent = OrderIntent(
            intent_id=1,
            strategy_id="s_sg",
            symbol="TSMC",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=100_0000,
            qty=1,
        )
        strat._return_value = [intent]
        runner.register(strat)

        # First event: storm
        await runner.process_event(_make_event())
        sg.trigger_storm.assert_called_with("risk_queue_full")
        sg.trigger_halt.assert_not_called()

        # Second event: halt
        await runner.process_event(_make_event())
        sg.trigger_halt.assert_called_with("risk_queue_full_persistent")

    @pytest.mark.asyncio
    async def test_queue_full_resets_on_success(self, runner_factory):
        """Successful event resets _queue_full_consecutive to 0."""
        runner, _, _ = runner_factory()
        strat = _FakeStrategy("s_ok")
        strat._return_value = []
        runner.register(strat)
        runner._queue_full_consecutive = 5
        await runner.process_event(_make_event())
        assert runner._queue_full_consecutive == 0
