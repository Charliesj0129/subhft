"""Behavior tests for strategy/runner.py."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.ops.session_governor import SessionPhase, TrackGate

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


def _make_bus(events=None):
    bus = MagicMock()
    events = events or []

    async def _gen():
        for e in events:
            yield e

    bus.consume.return_value = _gen()
    return bus


def _make_risk_queue():
    """Create a risk queue that uses put_nowait (not submit_nowait)."""
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


def _make_strategy(sid="strat_a", symbols=None, enabled=True):
    return _FakeStrategy(sid=sid, symbols=symbols, enabled=enabled)


def _make_event(symbol="TSMC", ts=123_000_000_000):
    return SimpleNamespace(symbol=symbol, ts=ts)


# Disable strategy registry auto-load and disable fail-fast compat
@pytest.fixture(autouse=True)
def _patch_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_STRATEGY_CONFIG", str(tmp_path / "empty.yaml"))
    (tmp_path / "empty.yaml").write_text("strategies: []\n")
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_RUST", "0")  # use Python circuit
    monkeypatch.setenv("HFT_STRATEGY_FEATURE_COMPAT_FAIL_FAST", "0")  # no hard fail


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
# Tests: constructor branches
# ---------------------------------------------------------------------------


def test_constructor_defaults(runner_factory):
    runner, _bus, rq = runner_factory()
    assert runner.running is False
    assert runner.strategies == []


def test_constructor_with_submit_nowait(runner_factory):
    rq = MagicMock()
    rq.submit_nowait = MagicMock()
    del rq.submit_typed_nowait
    runner, _, _ = runner_factory(rq=rq)
    assert runner._risk_submit is rq.submit_nowait


def test_constructor_obs_policy_balanced(monkeypatch, runner_factory):
    monkeypatch.setenv("HFT_OBS_POLICY", "balanced")
    runner, _, _ = runner_factory()
    assert runner._obs_policy == "balanced"
    assert runner._strategy_metrics_sample_every >= 1


def test_constructor_obs_policy_minimal(monkeypatch, runner_factory):
    monkeypatch.setenv("HFT_OBS_POLICY", "minimal")
    runner, _, _ = runner_factory()
    assert runner._obs_policy == "minimal"
    assert runner._diagnostic_metrics_enabled is False


def test_constructor_with_feature_engine(runner_factory):
    fe = MagicMock()
    fe.get_feature = MagicMock()
    fe.get_feature_view = MagicMock()
    fe.feature_set_id = "v1"
    fe.active_profile_id = "p1"
    fe.get_feature_tuple = MagicMock()
    runner, _, _ = runner_factory(feature_engine=fe)
    assert runner.feature_engine is fe


# ---------------------------------------------------------------------------
# Tests: register()
# ---------------------------------------------------------------------------


def test_register_strategy(runner_factory):
    runner, _, _ = runner_factory()
    strat = _make_strategy("my_strat")
    runner.register(strat)
    assert strat in runner.strategies
    assert "my_strat" in runner._strat_index


def test_register_strategy_tag_resolution(runner_factory):
    runner, _, _ = runner_factory()
    runner.symbol_metadata.symbols_by_tag["liquid"] = {"TSMC", "2330"}
    strat = _make_strategy("tag_strat", symbols=["tag:liquid"])
    runner.register(strat)
    assert strat in runner.strategies


def test_register_strategy_symbol_tags_attr(runner_factory):
    runner, _, _ = runner_factory()
    runner.symbol_metadata.symbols_by_tag["etf"] = {"0050"}
    strat = _make_strategy("tag_strat2", symbols=[])
    strat.symbol_tags = ["etf"]
    runner.register(strat)
    assert "0050" in strat.symbols


def test_register_compat_warning_non_fatal(runner_factory):
    runner, _, _ = runner_factory()
    strat = _make_strategy("warn_strat")
    with patch(
        "hft_platform.strategy.runner.check_strategy_feature_compat",
        return_value=[
            SimpleNamespace(level="warning", strategy_id="warn_strat", code="SOME_WARN", message="warn"),
        ],
    ):
        runner.register(strat)
    assert strat in runner.strategies


# ---------------------------------------------------------------------------
# Tests: _scale_price
# ---------------------------------------------------------------------------


def test_scale_price_int(runner_factory):
    runner, _, _ = runner_factory()
    result = runner._scale_price("TSMC", 100_0000)
    assert isinstance(result, int)


def test_scale_price_decimal(runner_factory):
    runner, _, _ = runner_factory()
    result = runner._scale_price("TSMC", Decimal("500"))
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# Tests: _intent_factory
# ---------------------------------------------------------------------------


def test_intent_factory_returns_order_intent(runner_factory):
    runner, _, _ = runner_factory()
    runner._typed_intent_fastpath = False
    intent = runner._intent_factory(
        strategy_id="s1",
        symbol="TSMC",
        side=1,
        price=1_000_000,
        qty=1,
        tif=0,
        intent_type=1,
    )
    assert intent is not None


def test_intent_factory_typed_fastpath(runner_factory):
    runner, _, _ = runner_factory()
    runner._typed_intent_fastpath = True
    runner._risk_submit_typed = MagicMock()
    intent = runner._intent_factory(
        strategy_id="s1",
        symbol="TSMC",
        side=1,
        price=1_000_000,
        qty=1,
        tif=0,
        intent_type=1,
    )
    assert isinstance(intent, tuple)
    assert intent[0] == "typed_intent_v1"


def test_intent_factory_uses_current_ts(runner_factory):
    runner, _, _ = runner_factory()
    runner._typed_intent_fastpath = False
    runner._current_source_ts_ns = 999_000_000
    intent = runner._intent_factory("s1", "TSMC", 1, 1000000, 1, 0, 1)
    assert intent.source_ts_ns == 999_000_000


# ---------------------------------------------------------------------------
# Tests: _build_positions_by_strategy
# ---------------------------------------------------------------------------


def test_build_positions_no_store(runner_factory):
    runner, _, _ = runner_factory()
    result = runner._build_positions_by_strategy()
    assert result == {}


def test_build_positions_rust_tracker(runner_factory):
    pos_store = MagicMock()
    rust_tracker = MagicMock()
    rust_tracker.get_positions_by_strategy.return_value = {"strat_a": {"TSMC": 10}}
    pos_store._rust_tracker = rust_tracker
    runner, _, _ = runner_factory(position_store=pos_store)
    result = runner._build_positions_by_strategy()
    assert result == {"strat_a": {"TSMC": 10}}


def test_build_positions_rust_tracker_fallback(runner_factory):
    pos_store = MagicMock()
    rust_tracker = MagicMock()
    rust_tracker.get_positions_by_strategy.side_effect = RuntimeError("fail")
    pos_store._rust_tracker = rust_tracker
    pos_store.positions = {}
    runner, _, _ = runner_factory(position_store=pos_store)
    result = runner._build_positions_by_strategy()
    assert isinstance(result, dict)


def test_build_positions_from_dict_key(runner_factory):
    pos_store = MagicMock()
    del pos_store._rust_tracker
    del pos_store.snapshot_positions  # force Python fallback path

    class _Pos:
        net_qty = 5

    pos_store.positions = {"pos:strat_a:TSMC": _Pos()}
    runner, _, _ = runner_factory(position_store=pos_store)
    result = runner._build_positions_by_strategy()
    assert "strat_a" in result
    assert result["strat_a"]["TSMC"] == 5


def test_build_positions_from_dict_object_value(runner_factory):
    pos_store = MagicMock()
    del pos_store._rust_tracker
    del pos_store.snapshot_positions  # force Python fallback path
    pos = MagicMock()
    pos.strategy_id = "strat_b"
    pos.symbol = "2330"
    pos.net_qty = 3
    pos_store.positions = {"anything": pos}
    runner, _, _ = runner_factory(position_store=pos_store)
    result = runner._build_positions_by_strategy()
    assert "strat_b" in result
    assert result["strat_b"]["2330"] == 3


def test_build_positions_fallback_wildcard(runner_factory):
    pos_store = MagicMock()
    del pos_store._rust_tracker
    del pos_store.snapshot_positions  # force Python fallback path
    pos_store.positions = {"unknown_key": 99}
    runner, _, _ = runner_factory(position_store=pos_store)
    result = runner._build_positions_by_strategy()
    assert "*" in result


# ---------------------------------------------------------------------------
# Tests: process_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_event_basic(runner_factory):
    runner, _, _ = runner_factory()
    strat = _make_strategy()
    runner.register(strat)
    event = _make_event()
    await runner.process_event(event)
    assert len(strat._calls) == 1


@pytest.mark.asyncio
async def test_process_event_with_intents(runner_factory):
    runner, _, rq = runner_factory()
    strat = _make_strategy()
    intent = SimpleNamespace(intent_type=1)
    strat._return_value = [intent]
    runner.register(strat)
    runner._typed_intent_fastpath = False
    event = _make_event()
    await runner.process_event(event)
    rq.put_nowait.assert_called_once_with(intent)


@pytest.mark.asyncio
async def test_process_event_typed_intent_fastpath(runner_factory):
    runner, _, rq = runner_factory()
    strat = _make_strategy()
    typed_intent = ("typed_intent_v1", 1, "s1", "TSMC", 1, 1, 1000000, 1, 0, "", 0, 0, "", "", "", 0)
    strat._return_value = [typed_intent]
    runner.register(strat)
    runner._typed_intent_fastpath = True
    runner._risk_submit_typed = MagicMock()
    event = _make_event()
    await runner.process_event(event)
    runner._risk_submit_typed.assert_called_once_with(typed_intent)


@pytest.mark.asyncio
async def test_process_event_typed_intent_fastpath_survives_track_gate_open(runner_factory):
    runner, _, _ = runner_factory()
    strat = _make_strategy()
    typed_intent = ("typed_intent_v1", 1, "s1", "TSMC", 1, 1, 1000000, 1, 0, "", 0, 0, "", "", "", 0)
    strat._return_value = [typed_intent]
    runner.register(strat)
    runner._typed_intent_fastpath = True
    runner._risk_submit_typed = MagicMock()
    gate = TrackGate()
    gate.register_symbol("TSMC", "stock")
    gate.set_track_phase("stock", SessionPhase.OPEN)
    runner.track_gate = gate

    event = _make_event()
    await runner.process_event(event)

    runner._risk_submit_typed.assert_called_once_with(typed_intent)


@pytest.mark.asyncio
async def test_process_event_typed_new_intent_blocked_in_close_only_without_crashing(runner_factory):
    runner, _, _ = runner_factory()
    strat = _make_strategy()
    typed_intent = ("typed_intent_v1", 1, "s1", "TSMC", 1, 1, 1000000, 1, 0, "", 0, 0, "", "", "", 0)
    strat._return_value = [typed_intent]
    runner.register(strat)
    runner._typed_intent_fastpath = True
    runner._risk_submit_typed = MagicMock()
    gate = TrackGate()
    gate.register_symbol("TSMC", "stock")
    gate.set_track_phase("stock", SessionPhase.CLOSE_ONLY)
    runner.track_gate = gate

    event = _make_event()
    await runner.process_event(event)

    runner._risk_submit_typed.assert_not_called()


@pytest.mark.asyncio
async def test_process_event_strategy_exception_triggers_circuit(runner_factory):
    runner, _, _ = runner_factory()

    class _ErrStrat(_FakeStrategy):
        def handle_event(self, ctx, event):
            raise RuntimeError("boom")

    strat = _ErrStrat("strat_a")
    runner.register(strat)
    runner._rust_circuit = None  # Force Python fallback path
    runner._circuit_threshold = 1
    event = _make_event()
    await runner.process_event(event)
    assert runner._failure_counts.get("strat_a", 0) >= 1


@pytest.mark.asyncio
async def test_process_event_halted_strategy_skipped(runner_factory):
    runner, _, _ = runner_factory()
    strat = _make_strategy(enabled=False)
    runner.register(strat)
    runner._rust_circuit = None  # Force Python fallback path
    runner._circuit_states["strat_a"] = "halted"
    from hft_platform.core import timebase

    runner._circuit_halted_at_ns["strat_a"] = timebase.now_ns()
    runner._circuit_cooldown_ns = int(3600 * 1e9)  # 1 hour
    event = _make_event()
    await runner.process_event(event)
    assert len(strat._calls) == 0


@pytest.mark.asyncio
async def test_process_event_circuit_recovery_cooldown(runner_factory):
    runner, _, _ = runner_factory()
    strat = _make_strategy(enabled=False)
    runner.register(strat)
    runner._rust_circuit = None  # Force Python fallback path
    runner._circuit_states["strat_a"] = "halted"
    runner._circuit_halted_at_ns["strat_a"] = 1  # epoch + 1ns
    runner._circuit_cooldown_ns = int(1e9)  # 1 second; definitely elapsed
    event = _make_event()
    await runner.process_event(event)
    assert strat.enabled is True


@pytest.mark.asyncio
async def test_process_event_degraded_recovery(runner_factory):
    runner, _, _ = runner_factory()
    strat = _make_strategy()
    runner.register(strat)
    runner._rust_circuit = None  # Force Python fallback path
    runner._circuit_states["strat_a"] = "degraded"
    runner._circuit_recovery_threshold = 1
    event = _make_event()
    await runner.process_event(event)
    assert runner._circuit_states.get("strat_a") == "normal"


@pytest.mark.asyncio
async def test_process_event_circuit_degraded_transition(runner_factory):
    runner, _, _ = runner_factory()

    class _ErrStrat(_FakeStrategy):
        def handle_event(self, ctx, event):
            raise RuntimeError("err")

    strat = _ErrStrat("strat_a")
    runner.register(strat)
    runner._rust_circuit = None  # Force Python fallback path
    runner._circuit_threshold = 4
    # After quarantine governor (DECISION-08), the first exception quarantines the
    # strategy and subsequent events are skipped (no circuit-breaker recording).
    # So only 1 failure is registered — not enough for degraded (half_threshold=2).
    # Disable quarantine to exercise the pure circuit-breaker path.
    runner.strategy_governor = None
    for _ in range(2):
        await runner.process_event(_make_event())
    assert runner._circuit_states.get("strat_a") == "degraded"


@pytest.mark.asyncio
async def test_process_event_delta_source_invalidates_positions(runner_factory):
    runner, _, _ = runner_factory()
    runner._positions_dirty = False
    event = SimpleNamespace(delta_source="fill", symbol="TSMC", ts=1)
    await runner.process_event(event)
    assert runner._positions_dirty is False  # was rebuilt


@pytest.mark.asyncio
async def test_process_event_targeted_dispatch(runner_factory):
    runner, _, _ = runner_factory()
    strat_a = _make_strategy("strat_a")
    strat_b = _make_strategy("strat_b")
    runner.register(strat_a)
    runner.register(strat_b)
    event = SimpleNamespace(symbol="TSMC", ts=1, strategy_id="strat_a")
    await runner.process_event(event)
    assert len(strat_a._calls) == 1
    assert len(strat_b._calls) == 0


# ---------------------------------------------------------------------------
# Tests: _emit_trace
# ---------------------------------------------------------------------------


def test_emit_trace_no_sampler(runner_factory):
    runner, _, _ = runner_factory()
    runner._trace_sampler = None
    runner._emit_trace("stage", "trace-id", {"key": "val"})
    assert runner._trace_sampler is None


def test_emit_trace_with_sampler(runner_factory):
    runner, _, _ = runner_factory()
    sampler = MagicMock()
    runner._trace_sampler = sampler
    runner._emit_trace("stage", "trace-id", {"key": "val"})
    sampler.emit.assert_called_once()


def test_emit_trace_sampler_raises(runner_factory):
    runner, _, _ = runner_factory()
    sampler = MagicMock()
    sampler.emit.side_effect = TypeError("bad")
    runner._trace_sampler = sampler
    runner._emit_trace("stage", "trace-id", {})
    sampler.emit.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: _extract_event_trace
# ---------------------------------------------------------------------------


def test_extract_event_trace_with_meta(runner_factory):
    runner, _, _ = runner_factory()
    meta = SimpleNamespace(local_ts=500_000, seq=42, topic="tick")
    event = SimpleNamespace(meta=meta, symbol="TSMC")
    ts, trace_id = runner._extract_event_trace(event)
    assert ts == 500_000
    assert trace_id == "tick:42"


def test_extract_event_trace_with_ts_attr(runner_factory):
    runner, _, _ = runner_factory()
    event = SimpleNamespace(ts=999_000, symbol="TSMC")
    ts, trace_id = runner._extract_event_trace(event)
    assert ts == 999_000


def test_extract_event_trace_no_ts(runner_factory):
    runner, _, _ = runner_factory()
    event = SimpleNamespace(symbol="TSMC")
    ts, trace_id = runner._extract_event_trace(event)
    assert ts > 0


# ---------------------------------------------------------------------------
# Tests: run() async loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_processes_finite_events(runner_factory):
    events = [_make_event("A"), _make_event("B")]
    bus = _make_bus(events)
    runner, _, _ = runner_factory(bus=bus)
    await asyncio.wait_for(runner.run(), timeout=5.0)
    # After processing all events the runner exits cleanly — bus was consumed
    bus.consume.assert_called_once()


@pytest.mark.asyncio
async def test_run_flush_on_cancel(runner_factory):
    bus = MagicMock()
    events_gen_exhausted = False

    async def _finite():
        nonlocal events_gen_exhausted
        yield _make_event()
        events_gen_exhausted = True

    bus.consume.return_value = _finite()
    runner, _, _ = runner_factory(bus=bus)
    await asyncio.wait_for(runner.run(), timeout=5.0)
    assert events_gen_exhausted


# ---------------------------------------------------------------------------
# Tests: obs_policy helper
# ---------------------------------------------------------------------------


def test_obs_policy_valid_values(monkeypatch):
    from hft_platform.strategy.runner import _obs_policy

    for val in ("minimal", "balanced", "debug"):
        monkeypatch.setenv("HFT_OBS_POLICY", val)
        assert _obs_policy() == val


def test_obs_policy_invalid(monkeypatch):
    from hft_platform.strategy.runner import _obs_policy

    monkeypatch.setenv("HFT_OBS_POLICY", "bogus")
    assert _obs_policy() == ""


# ---------------------------------------------------------------------------
# Tests: invalidate_positions
# ---------------------------------------------------------------------------


def test_invalidate_positions(runner_factory):
    runner, _, _ = runner_factory()
    runner._positions_dirty = False
    runner.invalidate_positions()
    assert runner._positions_dirty is True


# ---------------------------------------------------------------------------
# Tests: _flush_pending_strategy_metrics
# ---------------------------------------------------------------------------


def test_flush_pending_no_metrics(runner_factory):
    runner, _, _ = runner_factory()
    runner.metrics = None
    runner._strategy_pending_intents["s"] = 5
    runner._flush_pending_strategy_metrics()
    assert runner._strategy_pending_intents.get("s", 0) == 0


# ---------------------------------------------------------------------------
# Tests: tuple guard allows known typed ring events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tuple_guard_allows_known_tags(runner_factory):
    """Tuples starting with known typed ring tags should NOT be rejected."""
    runner, _, _ = runner_factory()
    runner.running = True

    for tag in ("tick", "bidask", "lobstats", "typed_intent_v1"):
        event = (tag, "TSMC", 100)
        with patch.object(runner, "_extract_event_trace", return_value=(0, "")) as mock_ext:
            await runner.process_event(event)
            assert mock_ext.call_count == 1, f"tag={tag!r} was rejected by guard"


@pytest.mark.asyncio
async def test_tuple_guard_rejects_unknown_tags(runner_factory):
    """Tuples with unknown first elements should be silently dropped."""
    runner, _, _ = runner_factory()
    runner.running = True

    event = ("unknown_tag", "TSMC", 100)
    with patch.object(runner, "_extract_event_trace", return_value=(0, "")) as mock_ext:
        await runner.process_event(event)
        assert mock_ext.call_count == 0, "unknown tag should be rejected by guard"


@pytest.mark.asyncio
async def test_tuple_guard_rejects_empty_tuple(runner_factory):
    """Empty tuples should be silently dropped."""
    runner, _, _ = runner_factory()
    runner.running = True

    with patch.object(runner, "_extract_event_trace", return_value=(0, "")) as mock_ext:
        await runner.process_event(())
        assert mock_ext.call_count == 0, "empty tuple should be rejected by guard"


# ---------------------------------------------------------------------------
# Tests: rejection feedback on risk_queue full
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejection_sink_receives_feedback_on_queue_full(runner_factory):
    """When risk_queue is full, a RiskFeedback with reason_code='risk_queue_full' is sent."""
    from hft_platform.contracts.strategy import IntentType, OrderIntent, RiskFeedback, Side

    # Build a risk queue that always raises QueueFull
    rq = MagicMock(spec=["put_nowait"])
    rq.put_nowait = MagicMock(side_effect=asyncio.QueueFull())

    runner, bus, _ = runner_factory(rq=rq)
    runner._rejection_sink = asyncio.Queue(maxsize=10)
    runner._typed_intent_fastpath = False

    # Create a minimal OrderIntent
    intent = OrderIntent(
        intent_id=42,
        strategy_id="strat_x",
        symbol="TSMC",
        side=Side.BUY,
        price=500_0000,
        qty=1,
        intent_type=IntentType.NEW,
    )

    # Directly invoke the submit path so QueueFull fires
    strat = _make_strategy("strat_x", symbols=["TSMC"])
    runner.register(strat)

    # Patch the strategy's handle_event to return the intent
    strat._return_value = [intent]

    event = _make_event(symbol="TSMC")
    runner.running = True
    with patch.object(runner, "_extract_event_trace", return_value=(0, "")):
        await runner.process_event(event)

    assert not runner._rejection_sink.empty(), "rejection sink should have received feedback"
    feedback = runner._rejection_sink.get_nowait()
    assert isinstance(feedback, RiskFeedback)
    assert feedback.reason_code == "risk_queue_full"
    assert feedback.strategy_id == "strat_x"
    assert feedback.symbol == "TSMC"


@pytest.mark.asyncio
async def test_rejection_sink_none_does_not_raise_on_queue_full(runner_factory):
    """When _rejection_sink is None, a full risk_queue should not raise."""
    from hft_platform.contracts.strategy import IntentType, OrderIntent, Side

    rq = MagicMock(spec=["put_nowait"])
    rq.put_nowait = MagicMock(side_effect=asyncio.QueueFull())

    runner, bus, _ = runner_factory(rq=rq)
    # _rejection_sink stays None (default)
    runner._typed_intent_fastpath = False

    intent = OrderIntent(
        intent_id=43,
        strategy_id="strat_y",
        symbol="TSMC",
        side=Side.BUY,
        price=500_0000,
        qty=1,
        intent_type=IntentType.NEW,
    )

    strat = _make_strategy("strat_y", symbols=["TSMC"])
    runner.register(strat)
    strat._return_value = [intent]

    event = _make_event(symbol="TSMC")
    runner.running = True
    # Should not raise
    with patch.object(runner, "_extract_event_trace", return_value=(0, "")):
        await runner.process_event(event)

    assert runner._rejection_sink is None
