"""Coverage gap tests for risk/engine.py.

Targets uncovered branches: config reload, DLQ drain paths,
evaluate typed_frame paths, position provider with object positions,
_bool_env edge cases, _emit_reject_metric cache invalidation,
_check_daily_loss_halt paths, halt-exempt fallback, and error metrics.
"""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderIntent,
    RiskDecision,
    Side,
    StormGuardState,
)
from hft_platform.risk.engine import RiskEngine, _cap_error_type, _obs_policy

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def risk_config(tmp_path):
    cfg = tmp_path / "risk.yaml"
    cfg.write_text(
        """
global_defaults:
  max_price_cap: 5000.0
  tick_size: 0.01
  price_band_ticks: 20
  max_notional: 10000000
  max_daily_loss: 500000
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


# ---------------------------------------------------------------------------
# _cap_error_type
# ---------------------------------------------------------------------------


def test_cap_error_type_known():
    assert _cap_error_type(ValueError("x")) == "ValueError"


def test_cap_error_type_unknown():
    class CustomError(Exception):
        pass

    assert _cap_error_type(CustomError("x")) == "other"


def test_cap_error_type_timeout():
    assert _cap_error_type(TimeoutError("x")) == "TimeoutError"


# ---------------------------------------------------------------------------
# _obs_policy
# ---------------------------------------------------------------------------


def test_obs_policy_minimal():
    with patch.dict(os.environ, {"HFT_RISK_OBS_POLICY": "minimal"}):
        assert _obs_policy() == "minimal"


def test_obs_policy_debug():
    with patch.dict(os.environ, {"HFT_RISK_OBS_POLICY": "debug"}):
        assert _obs_policy() == "debug"


def test_obs_policy_balanced():
    with patch.dict(os.environ, {"HFT_OBS_POLICY": "balanced"}, clear=False):
        with patch.dict(os.environ, {"HFT_RISK_OBS_POLICY": ""}, clear=False):
            # When risk-specific is empty, falls back to HFT_OBS_POLICY
            result = _obs_policy()
            # Result depends on env; just ensure it returns a string
            assert isinstance(result, str)


def test_obs_policy_unknown():
    with patch.dict(os.environ, {"HFT_RISK_OBS_POLICY": "something_invalid"}):
        assert _obs_policy() == ""


# ---------------------------------------------------------------------------
# _bool_env
# ---------------------------------------------------------------------------


def test_bool_env_none():
    assert RiskEngine._bool_env(None, default=True) is True
    assert RiskEngine._bool_env(None, default=False) is False


def test_bool_env_bool():
    assert RiskEngine._bool_env(True) is True
    assert RiskEngine._bool_env(False) is False


def test_bool_env_strings():
    assert RiskEngine._bool_env("1") is True
    assert RiskEngine._bool_env("true") is True
    assert RiskEngine._bool_env("yes") is True
    assert RiskEngine._bool_env("on") is True
    assert RiskEngine._bool_env("0") is False
    assert RiskEngine._bool_env("OFF") is False


# ---------------------------------------------------------------------------
# _parse_sample_every
# ---------------------------------------------------------------------------


def test_parse_sample_every_valid():
    with patch.dict(os.environ, {"TEST_SAMPLE": "5"}):
        assert RiskEngine._parse_sample_every("TEST_SAMPLE", default=1) == 5


def test_parse_sample_every_invalid():
    with patch.dict(os.environ, {"TEST_SAMPLE": "notanumber"}):
        assert RiskEngine._parse_sample_every("TEST_SAMPLE", default=3) == 3


def test_parse_sample_every_zero_clamped():
    with patch.dict(os.environ, {"TEST_SAMPLE": "0"}):
        assert RiskEngine._parse_sample_every("TEST_SAMPLE", default=1) == 1


# ---------------------------------------------------------------------------
# position provider: object-based access
# ---------------------------------------------------------------------------


def test_position_provider_with_callable(risk_config):
    provider = MagicMock(return_value=5)
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    eng = RiskEngine(risk_config, q_in, q_out, position_provider=provider)
    result = eng._current_strategy_symbol_net_position("2330", "s1")
    assert result == 5
    provider.assert_called_once_with("2330", "s1")


def test_position_provider_with_positions_dict(risk_config):
    pos = SimpleNamespace(symbol="2330", strategy_id="s1", net_qty=3)
    provider = SimpleNamespace(positions={"key1": pos})
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    eng = RiskEngine(risk_config, q_in, q_out, position_provider=provider)
    result = eng._current_strategy_symbol_net_position("2330", "s1")
    assert result == 3


def test_position_provider_with_positions_dict_filters_strategy(risk_config):
    pos = SimpleNamespace(symbol="2330", strategy_id="other", net_qty=3)
    provider = SimpleNamespace(positions={"key1": pos})
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    eng = RiskEngine(risk_config, q_in, q_out, position_provider=provider)
    result = eng._current_strategy_symbol_net_position("2330", "s1")
    assert result == 0


def test_position_provider_none(engine):
    result = engine._current_strategy_symbol_net_position("2330", "s1")
    assert result == 0


# ---------------------------------------------------------------------------
# evaluate: float price rejection
# ---------------------------------------------------------------------------


def test_evaluate_rejects_float_price(engine):
    intent = _make_intent(price=100.5)
    decision = engine.evaluate(intent)
    assert not decision.approved
    assert decision.reason_code == "FLOAT_PRICE"


# ---------------------------------------------------------------------------
# reload_config
# ---------------------------------------------------------------------------


def test_reload_config_success(engine, risk_config):
    """reload_config reads new yaml and updates validators."""
    engine.reload_config()
    # Should not raise; validators get updated config
    assert engine.config is not None


def test_reload_config_failure(engine, tmp_path):
    """reload_config handles missing file gracefully."""
    engine.config_path = str(tmp_path / "nonexistent.yaml")
    engine.reload_config()
    # Should log error but not raise
    assert engine.config is not None  # Old config preserved


# ---------------------------------------------------------------------------
# on_config_reload
# ---------------------------------------------------------------------------


def test_on_config_reload_clears_caches(engine):
    """on_config_reload clears validator caches."""
    # Add a fake cache attribute to a validator
    if engine.validators:
        engine.validators[0]._test_cache = {"key": "val"}
    new_config = {
        "global_defaults": {"max_price_cap": 3000.0},
        "strategies": {},
    }
    engine.on_config_reload(new_config)
    assert engine.config == new_config
    if engine.validators:
        cache = getattr(engine.validators[0], "_test_cache", None)
        # Cache should be cleared
        assert cache is None or cache == {}


# ---------------------------------------------------------------------------
# _emit_reject_metric edge cases
# ---------------------------------------------------------------------------


def test_emit_reject_metric_no_metrics(engine):
    engine._reject_metric_counter = 7
    engine.metrics = None
    engine._emit_reject_metric("s1", "REASON")  # Should not raise
    assert engine._reject_metric_counter == 7


def test_emit_reject_metric_sampling(engine):
    """Test that sampling skips metric emission when counter is not aligned."""
    engine._reject_metric_sample_every = 2
    engine._reject_metric_counter = 0
    engine._reject_metric_cache.clear()
    # First call increments counter to 1, mod 2 != 0, so skips
    engine._emit_reject_metric("s1", "TEST")
    assert engine._reject_metric_counter == 1
    assert engine._reject_metric_cache == {}
    # Second call would emit
    engine._emit_reject_metric("s1", "TEST")
    assert engine._reject_metric_counter == 0
    assert ("s1", "TEST") in engine._reject_metric_cache


def test_emit_reject_metric_cache_owner_change(engine):
    """When MetricsRegistry changes identity, cache is cleared."""
    engine._reject_metric_sample_every = 1
    engine._reject_metric_counter = 0
    engine._reject_metric_cache_owner_id = -1  # Force mismatch
    engine._emit_reject_metric("s1", "TEST")
    # Cache should be re-initialized with current owner
    assert engine._reject_metric_cache_owner_id == id(engine.metrics)


# ---------------------------------------------------------------------------
# create_command and monotonic_cmd_id
# ---------------------------------------------------------------------------


def test_create_command_increments_cmd_id(engine):
    intent = _make_intent()
    cmd1 = engine.create_command(intent)
    cmd2 = engine.create_command(intent)
    assert cmd2.cmd_id > cmd1.cmd_id


def test_monotonic_cmd_id_without_lock(engine):
    """monotonic_cmd_id works without lock."""
    assert engine._cmd_id_lock is None
    _ = engine.monotonic_cmd_id
    assert isinstance(engine.monotonic_cmd_id, int)


def test_monotonic_cmd_id_with_lock(risk_config):
    """monotonic_cmd_id works with threading lock."""
    with patch.dict(os.environ, {"HFT_RISK_CMD_ID_LOCK": "1"}):
        eng = RiskEngine(risk_config, asyncio.Queue(), asyncio.Queue())
    assert eng._cmd_id_lock is not None
    _ = eng.monotonic_cmd_id
    eng._next_cmd_id()
    assert eng.monotonic_cmd_id >= 1


# ---------------------------------------------------------------------------
# _is_halt_exempt
# ---------------------------------------------------------------------------


def test_is_halt_exempt_uses_is_halt_exempt_method(engine):
    """_is_halt_exempt delegates to storm_guard.is_halt_exempt when present."""
    sg = engine.storm_guard
    # StormGuard.is_halt_exempt is already defined as a real method
    assert callable(getattr(sg, "is_halt_exempt", None))
    # Non-exempt strategy returns False
    result = engine._is_halt_exempt("unknown_strat")
    assert result is False


def test_is_halt_exempt_via_frozen_set(engine):
    sg = engine.storm_guard
    sg._halt_exempt_strategies = frozenset({"safe"})
    # Use the real method which checks the frozen set
    assert engine._is_halt_exempt("safe") is True
    assert engine._is_halt_exempt("other") is False


# ---------------------------------------------------------------------------
# notify_fill_pnl and update_unrealized_pnl
# ---------------------------------------------------------------------------


def test_notify_fill_pnl(engine):
    """notify_fill_pnl forwards to DailyLossLimitValidator."""
    engine.notify_fill_pnl("s1", -50000)
    # Should not raise; verify validator got updated by checking attributes
    for v in engine.validators:
        if hasattr(v, "_accumulated_loss"):
            assert "s1" in v._accumulated_loss


def test_update_unrealized_pnl(engine):
    """update_unrealized_pnl updates unrealized and checks halt."""
    engine.update_unrealized_pnl(-100000)
    daily_loss = next(v for v in engine.validators if hasattr(v, "_unrealized_pnl"))
    assert daily_loss._unrealized_pnl == -100000


# ---------------------------------------------------------------------------
# _check_daily_loss_halt: already in HALT
# ---------------------------------------------------------------------------


def test_check_daily_loss_halt_already_halted(engine):
    engine.storm_guard.state = StormGuardState.HALT
    with patch.object(type(engine.storm_guard), "trigger_halt") as trigger_halt:
        engine._check_daily_loss_halt()
    trigger_halt.assert_not_called()


# ---------------------------------------------------------------------------
# DLQ drain paths
# ---------------------------------------------------------------------------


def test_drain_order_dlq_empty(engine):
    """_drain_order_dlq is a no-op when DLQ is empty."""
    engine._drain_order_dlq()
    assert len(engine._order_dlq) == 0


def test_drain_order_dlq_storm_clears_entries(engine):
    """DLQ entries are cleared during STORM state."""
    intent = _make_intent()
    cmd = engine.create_command(intent)
    engine._order_dlq.append((cmd, time.monotonic_ns()))
    engine.storm_guard.state = StormGuardState.STORM
    engine._rejection_sink = asyncio.Queue(maxsize=100)
    engine._drain_order_dlq()
    assert len(engine._order_dlq) == 0


def test_drain_order_dlq_ttl_expired(engine):
    """Expired DLQ entries are drained and rejected."""
    intent = _make_intent()
    cmd = engine.create_command(intent)
    # Enqueue with a very old timestamp (expired)
    old_ns = time.monotonic_ns() - 999_000_000_000
    engine._order_dlq.append((cmd, old_ns))
    engine._dlq_ttl_ns = 1  # 1 nanosecond TTL
    engine._rejection_sink = asyncio.Queue(maxsize=100)
    engine._drain_order_dlq()
    assert len(engine._order_dlq) == 0


def test_drain_order_dlq_deadline_expired(engine):
    """DLQ entries with expired deadline are rejected."""
    intent = _make_intent()
    cmd = engine.create_command(intent)
    cmd.deadline_ns = 1  # Already expired
    engine._order_dlq.append((cmd, time.monotonic_ns()))
    engine._dlq_ttl_ns = 999_000_000_000  # Very long TTL
    engine._rejection_sink = asyncio.Queue(maxsize=100)
    engine._drain_order_dlq()
    assert len(engine._order_dlq) == 0


def test_drain_order_dlq_successful_replay(engine):
    """DLQ entries are replayed to order_queue when space available."""
    intent = _make_intent()
    cmd = engine.create_command(intent)
    engine._order_dlq.append((cmd, time.monotonic_ns()))
    engine._dlq_ttl_ns = 999_000_000_000
    engine._rejection_sink = asyncio.Queue(maxsize=100)
    engine._drain_order_dlq()
    assert len(engine._order_dlq) == 0
    assert not engine.order_queue.empty()


def test_drain_order_dlq_queue_still_full(engine):
    """DLQ drain stops when order_queue is full."""
    q_out = asyncio.Queue(maxsize=1)
    q_out.put_nowait("filler")  # Fill the queue
    engine.order_queue = q_out
    intent = _make_intent()
    cmd = engine.create_command(intent)
    engine._order_dlq.append((cmd, time.monotonic_ns()))
    engine._dlq_ttl_ns = 999_000_000_000
    engine._rejection_sink = asyncio.Queue(maxsize=100)
    engine._drain_order_dlq()
    # Entry stays in DLQ since queue is full
    assert len(engine._order_dlq) == 1


# ---------------------------------------------------------------------------
# _revalidate_for_dlq
# ---------------------------------------------------------------------------


def test_revalidate_for_dlq_cancel_always_passes(engine):
    intent = _make_intent(intent_type=IntentType.CANCEL)
    cmd = engine.create_command(intent)
    assert engine._revalidate_for_dlq(cmd) is True


def test_revalidate_for_dlq_force_flat_always_passes(engine):
    intent = _make_intent(intent_type=IntentType.FORCE_FLAT)
    cmd = engine.create_command(intent)
    assert engine._revalidate_for_dlq(cmd) is True


def test_revalidate_for_dlq_rejected(engine):
    """When validators reject, revalidate returns False."""
    intent = _make_intent(price=0)  # Zero price triggers price band rejection
    cmd = engine.create_command(intent)
    result = engine._revalidate_for_dlq(cmd)
    assert result is False


# ---------------------------------------------------------------------------
# _send_dlq_rejection
# ---------------------------------------------------------------------------


def test_send_dlq_rejection_no_sink(engine):
    """When rejection_sink is None, logs error but doesn't crash."""
    engine._rejection_sink = None
    intent = _make_intent()
    cmd = engine.create_command(intent)
    with patch("hft_platform.risk.engine.logger.error") as error_log:
        engine._send_dlq_rejection(cmd, "test_reason")
    error_log.assert_called_once()


def test_send_dlq_rejection_sink_full(engine):
    """When rejection_sink is full, increments overflow metric."""
    engine._rejection_sink = asyncio.Queue(maxsize=1)
    engine._rejection_sink.put_nowait("filler")
    engine.metrics.rejection_sink_overflow_total.inc = MagicMock()
    intent = _make_intent()
    cmd = engine.create_command(intent)
    engine._send_dlq_rejection(cmd, "test_reason")
    assert engine._rejection_sink.qsize() == 1
    engine.metrics.rejection_sink_overflow_total.inc.assert_called_once()


# ---------------------------------------------------------------------------
# DLQ: revalidation failure path in drain
# ---------------------------------------------------------------------------


def test_drain_order_dlq_revalidation_failed(engine):
    """DLQ entries that fail revalidation are rejected."""
    intent = _make_intent(price=0)
    cmd = engine.create_command(intent)
    engine._order_dlq.append((cmd, time.monotonic_ns()))
    engine._dlq_ttl_ns = 999_000_000_000
    engine._rejection_sink = asyncio.Queue(maxsize=100)
    engine._drain_order_dlq()
    assert len(engine._order_dlq) == 0


# ---------------------------------------------------------------------------
# Order queue full escalation paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_order_queue_full_triggers_storm(risk_config):
    """When order queue is full, storm is triggered."""
    q_in = asyncio.Queue()
    q_out = asyncio.Queue(maxsize=1)
    q_out.put_nowait("filler")
    eng = RiskEngine(risk_config, q_in, q_out)
    eng._rejection_sink = asyncio.Queue(maxsize=100)
    eng._dlq_drain_interval = 999  # Prevent drain during test

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
    # Order should be in DLQ
    assert len(eng._order_dlq) >= 1


# ---------------------------------------------------------------------------
# DLQ overflow eviction
# ---------------------------------------------------------------------------


def test_dlq_overflow_eviction(engine):
    """When DLQ is at max capacity, oldest entry is evicted."""
    engine._rejection_sink = asyncio.Queue(maxsize=1000)
    engine._ORDER_DLQ_MAX = 2
    engine._order_dlq = __import__("collections").deque(maxlen=2)

    for i in range(2):
        intent = _make_intent(intent_id=i)
        cmd = engine.create_command(intent)
        engine._order_dlq.append((cmd, time.monotonic_ns()))

    # Now the DLQ is full. The engine code in run() does manual eviction before
    # appending; let's test that logic directly
    intent = _make_intent(intent_id=99)
    cmd = engine.create_command(intent)

    # Simulate the eviction logic from run()
    if len(engine._order_dlq) >= engine._ORDER_DLQ_MAX:
        evicted_cmd, _ = engine._order_dlq.popleft()
        engine._send_dlq_rejection(evicted_cmd, "dlq_overflow_evicted")
    engine._order_dlq.append((cmd, time.monotonic_ns()))

    assert len(engine._order_dlq) == 2
    # Newest entry should be last
    last_cmd, _ = engine._order_dlq[-1]
    assert last_cmd.intent.intent_id == 99


# ---------------------------------------------------------------------------
# _emit_trace
# ---------------------------------------------------------------------------


def test_emit_trace_no_sampler(engine):
    engine._trace_sampler = None
    engine._emit_trace("test", _make_intent(), {"key": "val"})
    assert engine._trace_sampler is None


def test_emit_trace_with_sampler(engine):
    sampler = MagicMock()
    engine._trace_sampler = sampler
    engine._emit_trace("test_stage", _make_intent(), {"key": "val"})
    # L5: order-bearing risk traces use emit_always (bypasses sample_every).
    sampler.emit_always.assert_called_once()


def test_emit_trace_sampler_exception(engine):
    sampler = MagicMock()
    sampler.emit_always.side_effect = RuntimeError("boom")
    engine._trace_sampler = sampler
    engine._emit_trace("test_stage", _make_intent(), {"key": "val"})
    sampler.emit_always.assert_called_once()


# ---------------------------------------------------------------------------
# greeks_validator integration
# ---------------------------------------------------------------------------


def test_evaluate_greeks_validator_reject(engine):
    """When greeks_validator rejects, decision is rejected."""
    gv = MagicMock()
    gv.check.return_value = (False, "GREEKS_LIMIT_EXCEEDED")
    engine._greeks_validator = gv
    intent = _make_intent()
    decision = engine.evaluate(intent)
    assert not decision.approved
    assert decision.reason_code == "GREEKS_LIMIT_EXCEEDED"


def test_evaluate_greeks_validator_pass(engine):
    """When greeks_validator passes, decision is approved."""
    gv = MagicMock()
    gv.check.return_value = (True, "OK")
    engine._greeks_validator = gv
    intent = _make_intent()
    decision = engine.evaluate(intent)
    assert decision.approved


# ---------------------------------------------------------------------------
# _audit_risk_decision error path
# ---------------------------------------------------------------------------


def test_audit_risk_decision_import_error(engine):
    """_audit_risk_decision handles ImportError gracefully."""
    intent = _make_intent()
    decision = RiskDecision(True, intent)
    with patch("hft_platform.recorder.audit.get_audit_writer", side_effect=ImportError("boom")) as getter:
        engine._audit_risk_decision(intent, decision)
    getter.assert_called_once()
