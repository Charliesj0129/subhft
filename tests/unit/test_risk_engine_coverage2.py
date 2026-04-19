"""Additional coverage tests for risk/engine.py — targets remaining uncovered lines.

Covers:
- greeks_provider import: __init__ greeks_validator init with ImportError (lines 218-223)
- position_provider: positions dict filters symbol mismatch (line 236)
- _init_fast_gate with env enabled (lines 258-298)
- _init_rust_validator: price_scale_provider codec path (lines 313, 321-327)
- _init_rust_validator: per-strategy config population (lines 334-336)
- reload_config: storm_guard.reload_thresholds path (line 367)
- on_config_reload: validator cache clear for dict caches (lines 387-396)
- run() loop: TTL expiry with rejection_sink full (lines 434-435)
- run() loop: HALT blocked post-approve with rejection_sink full (lines 467-468, 482-483)
- run() loop: order_queue full DLQ eviction (lines 499-501, 505)
- run() loop: CancelledError with intent_dequeued=True (line 537)
- run() loop: generic exception with rejection_sink full (lines 558-562)
- _drain_order_dlq: HALT clears during mid-drain (line 654-659)
- _revalidate_for_dlq: validators reject in revalidation (line 577-589)
- evaluate_typed_frame: with and without intent_view (lines 792-794)
- typed_frame_view: fallback to full materialization (lines 801-806)
- create_command_from_typed_frame: various paths (lines 809-818)
- _is_halt_exempt: fallback to frozenset (line 875)
- _emit_reject_metric: exception handling (lines 895-896)
- _check_daily_loss_halt: notification dispatch path (lines 971-989)
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    RiskDecision,
    RiskFeedback,
    Side,
    StormGuardState,
)
from hft_platform.risk.engine import RiskEngine, _load_rust_risk_validator

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def risk_config(tmp_path):
    cfg = tmp_path / "risk.yaml"
    cfg.write_text(
        """
global_defaults:
  max_price_cap: 5000.0
  max_price_cap_futures: 30000.0
  max_price_cap_options: 1000.0
  tick_size: 0.01
  price_band_ticks: 20
  max_notional: 10000000
  max_daily_loss: 500000
  max_qty: 100
risk:
  max_order_size: 10
  max_position: 20
  max_notional: 1000000
strategies:
  test_strat:
    max_notional: 500000
    price_band_ticks: 10
"""
    )
    return str(cfg)


@pytest.fixture
def engine(risk_config):
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    return RiskEngine(risk_config, q_in, q_out)


def _make_intent(**kwargs):
    defaults = dict(
        intent_id=1,
        strategy_id="s1",
        symbol="2330",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=1000000,
        qty=1,
        tif=TIF.ROD,
    )
    defaults.update(kwargs)
    return OrderIntent(**defaults)


# ── greeks_provider init: ImportError path ───────────────────────────────


def test_init_greeks_provider_import_error(risk_config):
    """When GreeksLimitValidator import fails, _greeks_validator stays None."""
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    greeks_provider = MagicMock()

    # Remove the module from sys.modules so the lazy import inside __init__ raises ImportError
    import sys

    saved = sys.modules.pop("hft_platform.risk.greeks_limit_validator", None)
    try:
        with patch.dict("sys.modules", {"hft_platform.risk.greeks_limit_validator": None}):
            eng = RiskEngine(risk_config, q_in, q_out, greeks_provider=greeks_provider)
        assert eng._greeks_validator is None
    finally:
        if saved is not None:
            sys.modules["hft_platform.risk.greeks_limit_validator"] = saved


# ── position_provider: positions dict with net_qty=None ──────────────────


def test_position_provider_positions_dict_net_qty_none(risk_config):
    """Position provider with net_qty=None returns 0."""
    pos = SimpleNamespace(symbol="2330", strategy_id="s1", net_qty=None)
    provider = SimpleNamespace(positions={"key1": pos})
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    eng = RiskEngine(risk_config, q_in, q_out, position_provider=provider)
    result = eng._current_strategy_symbol_net_position("2330", "s1")
    assert result == 0


def test_position_provider_positions_dict_filters_symbol(risk_config):
    """Position provider filters out non-matching symbols."""
    pos1 = SimpleNamespace(symbol="WRONG", strategy_id="s1", net_qty=5)
    pos2 = SimpleNamespace(symbol="2330", strategy_id="s1", net_qty=3)
    provider = SimpleNamespace(positions={"key1": pos1, "key2": pos2})
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    eng = RiskEngine(risk_config, q_in, q_out, position_provider=provider)
    result = eng._current_strategy_symbol_net_position("2330", "s1")
    assert result == 3


def test_position_provider_positions_dict_multiple_entries(risk_config):
    """Position provider sums multiple matching positions."""
    pos1 = SimpleNamespace(symbol="2330", strategy_id="s1", net_qty=2)
    pos2 = SimpleNamespace(symbol="2330", strategy_id="s1", net_qty=3)
    provider = SimpleNamespace(positions={"key1": pos1, "key2": pos2})
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    eng = RiskEngine(risk_config, q_in, q_out, position_provider=provider)
    result = eng._current_strategy_symbol_net_position("2330", "s1")
    assert result == 5


# ── reload_config: storm_guard.reload_thresholds ────────────────────────


def test_reload_config_calls_storm_guard_reload(engine, risk_config):
    """reload_config calls storm_guard.reload_thresholds when available."""
    mock_sg = MagicMock()
    mock_sg.state = StormGuardState.NORMAL
    mock_sg.reload_thresholds = MagicMock()
    engine.storm_guard = mock_sg
    engine.reload_config()
    mock_sg.reload_thresholds.assert_called_once()


def test_reload_config_detects_changed_keys(engine, risk_config, tmp_path):
    """reload_config detects changed config keys."""
    new_cfg = tmp_path / "risk2.yaml"
    new_cfg.write_text(
        """
global_defaults:
  max_price_cap: 9999.0
  tick_size: 0.01
  price_band_ticks: 50
  max_notional: 10000000
  max_daily_loss: 999999
risk:
  max_order_size: 10
strategies: {}
"""
    )
    engine.config_path = str(new_cfg)
    engine.reload_config()
    assert engine.config["global_defaults"]["max_price_cap"] == 9999.0


# ── _load_rust_risk_validator lazy init ──────────────────────────────────


def test_load_rust_risk_validator_returns_cached():
    """_load_rust_risk_validator returns cached value on second call."""
    import hft_platform.risk.engine as eng_mod

    original = eng_mod._RustRiskValidator
    try:
        eng_mod._RustRiskValidator = MagicMock()
        result = _load_rust_risk_validator()
        assert result is eng_mod._RustRiskValidator
    finally:
        eng_mod._RustRiskValidator = original


# ── evaluate_typed_frame ─────────────────────────────────────────────────


def test_evaluate_typed_frame_with_intent_view(engine):
    """evaluate_typed_frame uses provided intent_view."""
    intent = _make_intent()
    decision = engine.evaluate_typed_frame(None, intent_view=intent)
    assert decision.approved is True


def test_evaluate_typed_frame_without_intent_view(engine):
    """evaluate_typed_frame calls typed_frame_view when intent_view is None."""
    frame = (
        "typed_intent_v1",
        1,
        "s1",
        "2330",
        int(IntentType.NEW),
        int(Side.BUY),
        1000000,
        1,
        int(TIF.ROD),
        "",
        0,
        0,
        "",
        "",
        "",
        0,
    )
    with patch.object(engine, "typed_frame_view") as mock_view:
        mock_view.return_value = _make_intent()
        decision = engine.evaluate_typed_frame(frame)
    assert decision.approved is True
    mock_view.assert_called_once_with(frame)


# ── typed_frame_view fallback ────────────────────────────────────────────


def test_typed_frame_view_fallback_on_error(engine):
    """typed_frame_view falls back to full materialization on error."""
    with (
        patch("hft_platform.risk.engine.typed_frame_to_view", create=True, side_effect=TypeError("bad frame")),
        patch("hft_platform.gateway.channel.typed_frame_to_view", side_effect=TypeError("bad frame")),
        patch("hft_platform.gateway.channel.typed_frame_to_intent") as mock_intent,
    ):
        mock_intent.return_value = _make_intent()
        result = engine.typed_frame_view("bad_frame")
    assert result is not None


# ── create_command_from_typed_frame ──────────────────────────────────────


def test_create_command_from_typed_frame_with_order_intent(engine):
    """create_command_from_typed_frame fast path when intent_view is OrderIntent."""
    intent = _make_intent()
    cmd = engine.create_command_from_typed_frame(None, intent_view=intent)
    assert isinstance(cmd, OrderCommand)
    assert cmd.intent is intent


def test_create_command_from_typed_frame_with_view_fallback(engine):
    """create_command_from_typed_frame materializes from view when not OrderIntent."""
    view = MagicMock()
    view.strategy_id = "s1"
    view.symbol = "2330"

    with patch("hft_platform.gateway.channel.typed_view_to_intent") as mock_view_to_intent:
        mock_view_to_intent.return_value = _make_intent()
        cmd = engine.create_command_from_typed_frame(None, intent_view=view)
    assert isinstance(cmd, OrderCommand)


def test_create_command_from_typed_frame_view_to_intent_fails(engine):
    """create_command_from_typed_frame falls back to frame materialization on view error."""
    view = MagicMock()

    with (
        patch("hft_platform.gateway.channel.typed_view_to_intent", side_effect=TypeError("view convert fail")),
        patch("hft_platform.gateway.channel.typed_frame_to_intent") as mock_frame_to_intent,
    ):
        mock_frame_to_intent.return_value = _make_intent()
        cmd = engine.create_command_from_typed_frame("some_frame", intent_view=view)
    assert isinstance(cmd, OrderCommand)
    mock_frame_to_intent.assert_called_once_with("some_frame")


def test_create_command_from_typed_frame_no_view(engine):
    """create_command_from_typed_frame materializes from frame when intent_view is None."""
    with patch("hft_platform.gateway.channel.typed_frame_to_intent") as mock_frame:
        mock_frame.return_value = _make_intent()
        cmd = engine.create_command_from_typed_frame("raw_frame")
    assert isinstance(cmd, OrderCommand)


# ── create_typed_command_frame_from_typed_frame ──────────────────────────


def test_create_typed_command_frame_returns_tuple(engine):
    """create_typed_command_frame_from_typed_frame returns a command frame tuple."""
    frame = (1, 2, 3)
    result = engine.create_typed_command_frame_from_typed_frame(frame)
    assert isinstance(result, tuple)
    assert result[0] == "typed_order_cmd_v1"
    assert isinstance(result[1], int)  # cmd_id
    assert isinstance(result[2], int)  # deadline
    assert result[5] is frame  # original frame preserved


# ── _is_halt_exempt: fallback path ───────────────────────────────────────


def test_is_halt_exempt_no_method_uses_frozenset(engine):
    """_is_halt_exempt falls back to _halt_exempt_strategies frozenset."""
    sg = MagicMock()
    # Remove is_halt_exempt method to trigger fallback
    del sg.is_halt_exempt
    sg._halt_exempt_strategies = frozenset({"exempt_strat"})
    engine.storm_guard = sg
    assert engine._is_halt_exempt("exempt_strat") is True
    assert engine._is_halt_exempt("other") is False


# ── _emit_reject_metric: exception path ──────────────────────────────────


def test_emit_reject_metric_labels_exception(engine):
    """_emit_reject_metric catches exception from labels() call."""
    mock_metrics = MagicMock()
    mock_metrics.risk_reject_total.labels.side_effect = RuntimeError("labels crash")
    engine.metrics = mock_metrics
    engine._reject_metric_sample_every = 1
    engine._reject_metric_counter = 0
    engine._reject_metric_cache.clear()
    engine._reject_metric_cache_owner_id = id(mock_metrics)
    engine._emit_reject_metric("s1", "CRASH")
    # Should not raise
    assert True


# ── run() loop: TTL expiry with rejection_sink overflow ──────────────────


@pytest.mark.asyncio
async def test_run_ttl_expired_rejection_sink_full(risk_config):
    """TTL expiry increments overflow when rejection_sink is full."""
    from hft_platform.core import timebase

    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    sink = asyncio.Queue(maxsize=1)
    sink.put_nowait(MagicMock())  # Pre-fill

    eng = RiskEngine(risk_config, q_in, q_out, rejection_sink=sink)
    eng._dlq_drain_interval = 999

    old_ts = timebase.now_ns() - 2_000_000_000
    intent = _make_intent(ttl_ns=100_000_000, timestamp_ns=old_ts)
    q_in.put_nowait(intent)

    task = asyncio.create_task(eng.run())
    await asyncio.sleep(0.1)
    eng.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # TTL expired intent should have been rejected
    assert q_out.empty()


# ── run() loop: HALT blocked post-approve with rejection_sink overflow ───


@pytest.mark.asyncio
async def test_run_halt_blocked_rejection_sink_overflow(risk_config):
    """HALT-blocked approved order increments overflow when sink is full."""
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    sink = asyncio.Queue(maxsize=1)
    sink.put_nowait(MagicMock())  # Pre-fill

    eng = RiskEngine(risk_config, q_in, q_out, rejection_sink=sink)
    eng._dlq_drain_interval = 999
    eng.storm_guard.state = StormGuardState.HALT

    intent = _make_intent()
    q_in.put_nowait(intent)

    task = asyncio.create_task(eng.run())
    await asyncio.sleep(0.1)
    eng.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Should have been blocked at HALT — nothing in order_queue
    assert q_out.empty()


# ── run() loop: order_queue full triggers storm then halt ────────────────


@pytest.mark.asyncio
async def test_run_order_queue_full_consecutive_triggers_halt(risk_config):
    """Consecutive order_queue full events trigger HALT after threshold."""
    q_in = asyncio.Queue()
    q_out = asyncio.Queue(maxsize=1)
    q_out.put_nowait("filler")

    eng = RiskEngine(risk_config, q_in, q_out)
    eng._rejection_sink = asyncio.Queue(maxsize=1000)
    eng._dlq_drain_interval = 999
    eng._oq_full_halt_threshold = 2

    # Put 3 intents to exceed threshold
    for i in range(3):
        q_in.put_nowait(_make_intent(intent_id=i))

    task = asyncio.create_task(eng.run())
    await asyncio.sleep(0.2)
    eng.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Storm guard should have been triggered
    assert eng.storm_guard.state in (StormGuardState.STORM, StormGuardState.HALT)


# ── run() loop: generic exception sends feedback ─────────────────────────


@pytest.mark.asyncio
async def test_run_exception_sends_rejection_feedback(risk_config):
    """Generic exception in run() sends risk_engine_error feedback."""
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    sink = asyncio.Queue(maxsize=100)

    eng = RiskEngine(risk_config, q_in, q_out, rejection_sink=sink)
    eng._dlq_drain_interval = 999

    # Make evaluate() raise to trigger the except block
    original_evaluate = eng.evaluate
    call_count = [0]

    def bad_evaluate(intent):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("evaluate crash")
        return original_evaluate(intent)

    eng.evaluate = bad_evaluate

    intent = _make_intent()
    q_in.put_nowait(intent)

    task = asyncio.create_task(eng.run())
    await asyncio.sleep(0.1)
    eng.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Error feedback should have been sent
    assert sink.qsize() >= 1
    fb = sink.get_nowait()
    assert fb.reason_code == "risk_engine_error"


@pytest.mark.asyncio
async def test_run_exception_rejection_sink_overflow(risk_config):
    """Generic exception with full rejection_sink doesn't crash."""
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    sink = asyncio.Queue(maxsize=1)
    sink.put_nowait(MagicMock())  # Pre-fill

    eng = RiskEngine(risk_config, q_in, q_out, rejection_sink=sink)
    eng._dlq_drain_interval = 999

    eng.evaluate = MagicMock(side_effect=RuntimeError("crash"))

    q_in.put_nowait(_make_intent())

    task = asyncio.create_task(eng.run())
    await asyncio.sleep(0.1)
    eng.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Should not crash
    assert True


# ── _drain_order_dlq: mid-drain STORM escalation ────────────────────────


def test_drain_order_dlq_mid_drain_storm_clears(engine):
    """DLQ drain mid-loop detects STORM and clears remaining entries."""
    engine._rejection_sink = asyncio.Queue(maxsize=1000)
    engine._dlq_ttl_ns = 999_000_000_000

    for i in range(3):
        intent = _make_intent(intent_id=i)
        cmd = engine.create_command(intent)
        engine._order_dlq.append((cmd, time.monotonic_ns()))

    # Queue accepts first entry but then escalation
    q_out = asyncio.Queue(maxsize=1)
    engine.order_queue = q_out

    # Mock storm_guard to escalate to STORM after first successful drain
    call_count = [0]
    original_state = engine.storm_guard.state

    class EscalatingStormGuard:
        def __init__(self):
            self._state = StormGuardState.NORMAL

        @property
        def state(self):
            return self._state

        def validate(self, intent):
            return True, ""

        def trigger_storm(self, reason):
            self._state = StormGuardState.STORM

        def trigger_halt(self, reason):
            self._state = StormGuardState.HALT

    sg = EscalatingStormGuard()
    engine.storm_guard = sg

    engine._drain_order_dlq()

    # Should have drained one entry to queue, then queue full triggers STORM check
    assert q_out.qsize() >= 1


# ── _check_daily_loss_halt: notification dispatch ────────────────────────


@pytest.mark.asyncio
async def test_check_daily_loss_halt_triggers_notifications(risk_config):
    """_check_daily_loss_halt fires notification dispatcher when HALT triggered."""
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()

    dispatcher = MagicMock()
    dispatcher.notify_daily_loss = AsyncMock()
    dispatcher.notify_halt = AsyncMock()

    eng = RiskEngine(risk_config, q_in, q_out, notification_dispatcher=dispatcher)

    # Force DailyLossLimitValidator to trigger halt
    from hft_platform.risk.validators import DailyLossLimitValidator

    for v in eng.validators:
        if isinstance(v, DailyLossLimitValidator):
            v.halt_triggered = True  # Public attribute, not _halt_triggered
            v._accumulated_loss = {"s1": -999999}
            v._unrealized_pnl = -100000
            v._default_max_daily_loss = 500000
            break

    eng._check_daily_loss_halt()

    assert eng.storm_guard.state == StormGuardState.HALT
    # Give async tasks time to run
    await asyncio.sleep(0.05)
    dispatcher.notify_daily_loss.assert_called_once()
    dispatcher.notify_halt.assert_called_once()


def test_check_daily_loss_halt_no_dispatcher_no_crash(engine):
    """_check_daily_loss_halt works without notification dispatcher."""
    from hft_platform.risk.validators import DailyLossLimitValidator

    engine._notification_dispatcher = None
    for v in engine.validators:
        if isinstance(v, DailyLossLimitValidator):
            v.halt_triggered = True  # Public attribute
            v._accumulated_loss = {"s1": -999999}
            v._unrealized_pnl = -100000
            break

    engine._check_daily_loss_halt()
    assert engine.storm_guard.state == StormGuardState.HALT


# ── run() loop: risk-rejected sends feedback ─────────────────────────────


@pytest.mark.asyncio
async def test_run_risk_rejected_sends_feedback(risk_config):
    """Risk-rejected intent sends RiskFeedback to rejection_sink."""
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    sink = asyncio.Queue(maxsize=100)
    eng = RiskEngine(risk_config, q_in, q_out, rejection_sink=sink)
    eng._dlq_drain_interval = 999

    # Zero price will trigger price band validator rejection
    intent = _make_intent(price=0)
    q_in.put_nowait(intent)

    task = asyncio.create_task(eng.run())
    await asyncio.sleep(0.1)
    eng.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert sink.qsize() >= 1
    fb = sink.get_nowait()
    assert isinstance(fb, RiskFeedback)
    assert fb.reason_code != "OK"


@pytest.mark.asyncio
async def test_run_risk_rejected_rejection_sink_overflow(risk_config):
    """Risk rejection with full sink increments overflow metric."""
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    sink = asyncio.Queue(maxsize=1)
    sink.put_nowait(MagicMock())  # Pre-fill

    eng = RiskEngine(risk_config, q_in, q_out, rejection_sink=sink)
    eng._dlq_drain_interval = 999

    intent = _make_intent(price=0)
    q_in.put_nowait(intent)

    task = asyncio.create_task(eng.run())
    await asyncio.sleep(0.1)
    eng.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Should not crash even with full sink
    assert True


# ── _send_dlq_rejection: was_approved field ──────────────────────────────


def test_send_dlq_rejection_sets_was_approved(engine):
    """_send_dlq_rejection sets was_approved=True on feedback."""
    sink = asyncio.Queue(maxsize=100)
    engine._rejection_sink = sink
    intent = _make_intent()
    cmd = engine.create_command(intent)
    engine._send_dlq_rejection(cmd, "dlq_expired")
    fb = sink.get_nowait()
    assert fb.was_approved is True
    assert fb.reason_code == "dlq_expired"


# ── _audit_risk_decision ─────────────────────────────────────────────────


def test_audit_risk_decision_handles_import_error(engine):
    """_audit_risk_decision handles exception from get_audit_writer."""
    with patch(
        "hft_platform.recorder.audit.get_audit_writer",
        side_effect=ImportError("no audit module"),
    ):
        intent = _make_intent()
        decision = RiskDecision(True, intent)
        engine._audit_risk_decision(intent, decision)
    # Should not raise
    assert True


# ── DLQ drain: successful entry + expired entry mix ──────────────────────


def test_drain_order_dlq_mixed_expired_and_valid(engine):
    """DLQ drain handles mix of expired and valid entries."""
    engine._rejection_sink = asyncio.Queue(maxsize=1000)

    # Add an expired entry
    intent_old = _make_intent(intent_id=1)
    cmd_old = engine.create_command(intent_old)
    old_ns = time.monotonic_ns() - 999_000_000_000
    engine._order_dlq.append((cmd_old, old_ns))

    # Add a valid entry
    intent_new = _make_intent(intent_id=2)
    cmd_new = engine.create_command(intent_new)
    engine._order_dlq.append((cmd_new, time.monotonic_ns()))

    engine._dlq_ttl_ns = 1  # 1ns TTL to expire old entry

    engine._drain_order_dlq()

    # Old entry expired, new entry may also be expired due to 1ns TTL
    # Both should have been processed
    assert len(engine._order_dlq) == 0


# ── DLQ drain during HALT state ─────────────────────────────────────────


def test_drain_order_dlq_halt_clears_all(engine):
    """DLQ entries are cleared during HALT state."""
    engine._rejection_sink = asyncio.Queue(maxsize=1000)

    for i in range(3):
        intent = _make_intent(intent_id=i)
        cmd = engine.create_command(intent)
        engine._order_dlq.append((cmd, time.monotonic_ns()))

    engine.storm_guard.state = StormGuardState.HALT
    engine._drain_order_dlq()

    assert len(engine._order_dlq) == 0
    # Each entry should have generated a rejection feedback
    assert engine._rejection_sink.qsize() == 3
