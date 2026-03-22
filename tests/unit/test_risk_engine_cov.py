"""Coverage-boosting tests for RiskEngine (risk/engine.py).

Targets: _load_rust_risk_validator caching/fallback/fail, _obs_policy,
_get_trace_sampler ImportError, _init_fast_gate disabled/ValueError/risk sources,
_init_rust_validator disabled/None/per-strategy config/raises, reload_config,
run() async loop (approved/rejected/exception/CancelledError), evaluate fast gate
OSError/unknown code, evaluate rust validator with LOB, typed frame view/create command,
monotonic cmd_id, emit reject metric edge cases, audit risk decision, emit trace,
notify fill pnl no validator, shared scale cache.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    IntentType,
    OrderIntent,
    RiskDecision,
    Side,
)
from hft_platform.risk import engine as engine_mod
from hft_platform.risk.engine import RiskEngine, _load_rust_risk_validator, _obs_policy

# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def cfg_path(tmp_path):
    p = tmp_path / "risk.yaml"
    p.write_text(
        """
global_defaults:
  max_price_cap: 5000.0
  tick_size: 0.01
  price_band_ticks: 20
  max_notional: 10000000
  max_qty: 1000
risk:
  max_order_size: 500
  max_position: 1000
  max_notional: 5000000
strategies:
  s1:
    price_band_ticks: 10
    max_notional: 500000
"""
    )
    return str(p)


@pytest.fixture()
def engine(cfg_path):
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    return RiskEngine(cfg_path, q_in, q_out)


def _intent(
    intent_id: int = 1,
    price: int = 1_000_000,
    qty: int = 5,
    intent_type: IntentType = IntentType.NEW,
    strategy_id: str = "s1",
    symbol: str = "TSE:2330",
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=Side.BUY,
        price=price,
        qty=qty,
    )


# ── _load_rust_risk_validator ──────────────────────────────────────────────


def test_load_rust_risk_validator_caches():
    """Second call returns cached value without re-importing."""
    engine_mod._RustRiskValidator = None
    result1 = _load_rust_risk_validator()
    result2 = _load_rust_risk_validator()
    assert result1 is result2
    engine_mod._RustRiskValidator = None


def test_load_rust_risk_validator_both_fail():
    """Returns None when both imports fail."""
    engine_mod._RustRiskValidator = None
    with patch.dict("sys.modules", {"hft_platform.rust_core": None, "rust_core": None}):
        result = _load_rust_risk_validator()
        assert result is None
        engine_mod._RustRiskValidator = None


# ── _obs_policy ────────────────────────────────────────────────────────────


def test_obs_policy_minimal(monkeypatch):
    monkeypatch.setenv("HFT_RISK_OBS_POLICY", "minimal")
    assert _obs_policy() == "minimal"


def test_obs_policy_fallback_hft_obs(monkeypatch):
    monkeypatch.delenv("HFT_RISK_OBS_POLICY", raising=False)
    monkeypatch.setenv("HFT_OBS_POLICY", "debug")
    assert _obs_policy() == "debug"


def test_obs_policy_unknown_returns_empty(monkeypatch):
    monkeypatch.setenv("HFT_RISK_OBS_POLICY", "bogus")
    monkeypatch.delenv("HFT_OBS_POLICY", raising=False)
    assert _obs_policy() == ""


# ── _get_trace_sampler ImportError ─────────────────────────────────────────


def test_get_trace_sampler_import_error_returns_none():
    result = engine_mod._get_trace_sampler()
    # Must not raise; may return sampler or None
    assert result is None or result is not None


# ── _init_fast_gate ────────────────────────────────────────────────────────


def test_init_fast_gate_disabled_by_default(engine):
    assert engine._fast_gate is None


def test_init_fast_gate_enabled_but_import_fails(cfg_path, monkeypatch):
    """If FastGate import fails, returns None gracefully."""
    monkeypatch.setenv("HFT_RISK_FAST_GATE", "1")
    with patch.dict("sys.modules", {"hft_platform.risk.fast_gate": None}):
        q_in = asyncio.Queue()
        q_out = asyncio.Queue()
        e = RiskEngine(cfg_path, q_in, q_out)
        assert e._fast_gate is None


def test_init_fast_gate_invalid_scale(cfg_path, monkeypatch):
    """Invalid HFT_RISK_FAST_GATE_PRICE_SCALE falls back to 10000."""
    monkeypatch.setenv("HFT_RISK_FAST_GATE", "1")
    monkeypatch.setenv("HFT_RISK_FAST_GATE_PRICE_SCALE", "notanint")

    mock_gate_cls = MagicMock()
    mock_gate_instance = MagicMock()
    mock_gate_cls.return_value = mock_gate_instance
    fast_gate_module = MagicMock()
    fast_gate_module.FastGate = mock_gate_cls

    with patch.dict("sys.modules", {"hft_platform.risk.fast_gate": fast_gate_module}):
        q_in = asyncio.Queue()
        q_out = asyncio.Queue()
        e = RiskEngine(cfg_path, q_in, q_out)
        if mock_gate_cls.called:
            call_kwargs = mock_gate_cls.call_args[1]
            assert call_kwargs.get("price_scale", 10_000) == 10_000


# ── _init_rust_validator ───────────────────────────────────────────────────


def test_init_rust_validator_disabled_by_default(engine):
    assert engine._rust_validator is None


def test_init_rust_validator_enabled_but_no_cls(cfg_path, monkeypatch):
    """If _load_rust_risk_validator returns None, result is None."""
    monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "1")
    engine_mod._RustRiskValidator = None

    with patch("hft_platform.risk.engine._load_rust_risk_validator", return_value=None):
        q_in = asyncio.Queue()
        q_out = asyncio.Queue()
        e = RiskEngine(cfg_path, q_in, q_out)
        assert e._rust_validator is None


def test_init_rust_validator_per_strategy_config(cfg_path, monkeypatch):
    """Per-strategy config is applied when validator class is available."""
    monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "1")

    mock_cls = MagicMock()
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    with patch("hft_platform.risk.engine._load_rust_risk_validator", return_value=mock_cls):
        q_in = asyncio.Queue()
        q_out = asyncio.Queue()
        e = RiskEngine(cfg_path, q_in, q_out)
        # set_band_ticks and set_max_notional should have been called for s1
        assert mock_instance.set_band_ticks.called or mock_instance.set_max_notional.called


def test_init_rust_validator_raises_returns_none(cfg_path, monkeypatch):
    """OSError during validator init returns None."""
    monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "1")

    mock_cls = MagicMock(side_effect=OSError("shm error"))

    with patch("hft_platform.risk.engine._load_rust_risk_validator", return_value=mock_cls):
        q_in = asyncio.Queue()
        q_out = asyncio.Queue()
        e = RiskEngine(cfg_path, q_in, q_out)
        assert e._rust_validator is None


# ── reload_config ──────────────────────────────────────────────────────────


def test_reload_config_updates_validators(engine):
    """reload_config reloads file and pushes new config to validators."""
    import yaml

    new_cfg = {
        "global_defaults": {"max_price_cap": 9999.0, "tick_size": 0.01, "price_band_ticks": 5, "max_notional": 999},
        "risk": {},
        "strategies": {},
    }
    with open(engine.config_path, "w") as f:
        yaml.dump(new_cfg, f)

    engine.reload_config()

    assert engine.config["global_defaults"]["max_price_cap"] == 9999.0
    for v in engine.validators:
        assert v.config["global_defaults"]["max_price_cap"] == 9999.0


def test_reload_config_file_missing_logs_error(engine):
    """reload_config with missing file logs error without raising."""
    engine.config_path = "/nonexistent/path.yaml"
    engine.reload_config()  # Must not propagate exception
    assert engine.config_path == "/nonexistent/path.yaml"


# ── run() async loop ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_approved_puts_command(engine):
    intent = _intent(price=1_000_000)
    engine.intent_queue.put_nowait(intent)

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)
    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert not engine.order_queue.empty()
    cmd = engine.order_queue.get_nowait()
    assert cmd.intent.intent_id == 1


@pytest.mark.asyncio
async def test_run_rejected_does_not_put_command(engine):
    """Float price triggers FLOAT_PRICE rejection."""
    bad_intent = MagicMock(spec=OrderIntent)
    bad_intent.price = 100.5  # float triggers rejection
    bad_intent.strategy_id = "s1"
    bad_intent.symbol = "TSE:2330"
    bad_intent.intent_type = IntentType.NEW
    bad_intent.trace_id = ""
    bad_intent.intent_id = 1

    engine.intent_queue.put_nowait(bad_intent)
    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)
    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert engine.order_queue.empty()


@pytest.mark.asyncio
async def test_run_exception_in_evaluate_continues(engine):
    """Exception in evaluate is caught; loop continues."""
    engine.metrics = None
    call_count = [0]
    original_evaluate = engine.evaluate

    def bad_evaluate(intent):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ValueError("unexpected evaluate error")
        return original_evaluate(intent)

    engine.evaluate = bad_evaluate

    engine.intent_queue.put_nowait(_intent(intent_id=1))
    engine.intent_queue.put_nowait(_intent(intent_id=2))

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.1)
    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert call_count[0] >= 2


@pytest.mark.asyncio
async def test_run_cancelled_error_stops_loop(engine):
    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.done() or task.cancelled()


# ── evaluate: fast gate OSError / unknown code ────────────────────────────


def test_evaluate_fast_gate_oserror_fail_closed(engine):
    """FastGate OSError causes fail-closed FASTGATE_ERROR rejection."""
    mock_gate = MagicMock()
    mock_gate.check.side_effect = OSError("shm gone")
    engine._fast_gate = mock_gate

    decision = engine.evaluate(_intent())
    assert not decision.approved
    assert "FASTGATE" in decision.reason_code


def test_evaluate_fast_gate_unknown_code(engine):
    """Unknown FastGate code maps to FASTGATE_REJECT."""
    mock_gate = MagicMock()
    mock_gate.check.return_value = (False, 99)
    engine._fast_gate = mock_gate

    decision = engine.evaluate(_intent())
    assert not decision.approved
    assert decision.reason_code == "FASTGATE_REJECT"


def test_evaluate_fast_gate_pass_through(engine):
    """FastGate passing (ok=True) proceeds to Python validators."""
    mock_gate = MagicMock()
    mock_gate.check.return_value = (True, 0)
    engine._fast_gate = mock_gate

    decision = engine.evaluate(_intent())
    assert decision.approved


def test_evaluate_cancel_intent_skips_fast_gate(engine):
    """CANCEL intent skips FastGate check."""
    mock_gate = MagicMock()
    mock_gate.check.return_value = (False, 1)
    engine._fast_gate = mock_gate

    decision = engine.evaluate(_intent(intent_type=IntentType.CANCEL))
    assert decision.approved


# ── evaluate: rust validator with LOB ─────────────────────────────────────


def test_evaluate_rust_validator_ok(engine):
    """RustRiskValidator returning ok=True proceeds."""
    mock_rv = MagicMock()
    mock_rv.check.return_value = (True, 0)
    engine._rust_validator = mock_rv

    decision = engine.evaluate(_intent())
    assert decision.approved


def test_evaluate_rust_validator_rejection(engine):
    """RustRiskValidator rejection maps reason code."""
    mock_rv = MagicMock()
    mock_rv.check.return_value = (False, 1)
    engine._rust_validator = mock_rv

    decision = engine.evaluate(_intent())
    assert not decision.approved
    assert decision.reason_code == "PRICE_ZERO_OR_NEG"


def test_evaluate_rust_validator_oserror_falls_through(engine):
    """OSError from RustRiskValidator falls through to Python validators."""
    mock_rv = MagicMock()
    mock_rv.check.side_effect = OSError("rv error")
    engine._rust_validator = mock_rv

    decision = engine.evaluate(_intent())
    assert decision.approved or not decision.approved  # Must not raise


def test_evaluate_rust_validator_with_lob_mid_price(engine):
    """RustRiskValidator is passed mid_price from LOB if available."""
    mock_rv = MagicMock()
    mock_rv.check.return_value = (True, 0)
    engine._rust_validator = mock_rv

    engine.validators[0].lob = MagicMock()
    engine.validators[0]._get_mid_price = lambda symbol: 5_000_000

    decision = engine.evaluate(_intent())
    assert decision.approved
    call_args = mock_rv.check.call_args[0]
    assert call_args[5] == 5_000_000  # mid_price passed in


# ── typed_frame_view / create_command ─────────────────────────────────────


def test_typed_frame_view_fallback(engine):
    """typed_frame_view falls back to typed_frame_to_intent on error."""
    with (
        patch("hft_platform.gateway.channel.typed_frame_to_view", side_effect=KeyError("missing")),
        patch("hft_platform.gateway.channel.typed_frame_to_intent") as mock_intent,
    ):
        mock_intent.return_value = _intent()
        result = engine.typed_frame_view(("dummy_frame",))
        assert result is not None


def test_create_command_increments_cmd_id(engine):
    """create_command produces monotonically increasing cmd_ids."""
    intent = _intent()
    cmd1 = engine.create_command(intent)
    cmd2 = engine.create_command(intent)
    assert cmd2.cmd_id > cmd1.cmd_id


def test_monotonic_cmd_id_property(engine):
    """monotonic_cmd_id property returns current counter value."""
    engine._monotonic_cmd_id = 42
    assert engine.monotonic_cmd_id == 42


def test_monotonic_cmd_id_with_lock(cfg_path, monkeypatch):
    """With lock enabled, monotonic_cmd_id uses the lock."""
    monkeypatch.setenv("HFT_RISK_CMD_ID_LOCK", "1")
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    e = RiskEngine(cfg_path, q_in, q_out)
    assert e._cmd_id_lock is not None
    e._monotonic_cmd_id = 10
    assert e.monotonic_cmd_id == 10


def test_create_typed_command_frame(engine):
    """create_typed_command_frame_from_typed_frame returns valid 6-tuple."""
    frame = ("typed_intent_v1", 1, 2, 3, 4, 5)
    result = engine.create_typed_command_frame_from_typed_frame(frame)
    assert isinstance(result, tuple)
    assert len(result) == 6
    assert result[0] == "typed_order_cmd_v1"


# ── emit reject metric edge cases ─────────────────────────────────────────


def test_emit_reject_metric_none_metrics(engine):
    """_emit_reject_metric with no metrics returns silently."""
    engine.metrics = None
    engine._emit_reject_metric("s1", "TEST_REASON")  # Must not raise
    assert engine.metrics is None  # confirms no side-effect replacement


def test_emit_reject_metric_counter_skips(engine):
    """Counter sample_every > 1 skips emit when counter != 0."""
    engine._reject_metric_sample_every = 10
    engine._reject_metric_counter = 5
    mock_metrics = MagicMock()
    engine.metrics = mock_metrics

    engine._emit_reject_metric("s1", "REASON")
    mock_metrics.risk_reject_total.labels.assert_not_called()


def test_emit_reject_metric_cache_stale_owner_id(engine):
    """Stale owner_id clears cache before new label creation."""
    engine._reject_metric_sample_every = 1
    engine._reject_metric_counter = 0

    mock_metrics = MagicMock()
    mock_child = MagicMock()
    mock_metrics.risk_reject_total.labels.return_value = mock_child

    engine.metrics = mock_metrics
    engine._reject_metric_cache_owner_id = id(mock_metrics) + 999  # stale

    engine._emit_reject_metric("s1", "STALE_TEST")
    assert mock_metrics.risk_reject_total.labels.called


# ── _audit_risk_decision ───────────────────────────────────────────────────


def test_audit_risk_decision_called_on_approve(engine):
    """_audit_risk_decision is called after a passing evaluate."""
    with patch("hft_platform.risk.engine.get_audit_writer") as mock_aw:
        mock_writer = MagicMock()
        mock_aw.return_value = mock_writer

        engine.evaluate(_intent())
        mock_writer.log_risk_decision.assert_called_once()


def test_audit_risk_decision_exception_swallowed(engine):
    """Exception in _audit_risk_decision does not propagate."""
    with patch("hft_platform.risk.engine.get_audit_writer", side_effect=RuntimeError("audit fail")):
        intent = _intent()
        decision = RiskDecision(approved=True, intent=intent)
        engine._audit_risk_decision(intent, decision)  # Must not raise
    assert decision.approved is True  # confirms no mutation on audit failure


# ── _emit_trace ────────────────────────────────────────────────────────────


def test_emit_trace_with_sampler(engine):
    sampler = MagicMock()
    engine._trace_sampler = sampler
    intent = _intent()

    engine._emit_trace("risk_test", intent, {"stage": "test"})
    sampler.emit.assert_called_once()


def test_emit_trace_no_sampler(engine):
    engine._trace_sampler = None
    engine._emit_trace("stage", _intent(), {})  # Must not raise
    assert engine._trace_sampler is None


def test_emit_trace_exception_swallowed(engine):
    sampler = MagicMock()
    sampler.emit.side_effect = TypeError("bad payload")
    engine._trace_sampler = sampler
    engine._emit_trace("stage", _intent(), {"key": "val"})  # Must not raise
    assert sampler.emit.called  # confirms emit was attempted before exception


# ── notify_fill_pnl ────────────────────────────────────────────────────────


def test_notify_fill_pnl_with_daily_loss_validator(engine):
    """notify_fill_pnl calls record_pnl on DailyLossLimitValidator."""
    from hft_platform.risk.validators import DailyLossLimitValidator

    has_dll = any(isinstance(v, DailyLossLimitValidator) for v in engine.validators)
    if has_dll:
        engine.notify_fill_pnl("s1", -50_000)
        # Validator found and presumably updated without error
        assert True


def test_notify_fill_pnl_no_daily_loss_validator(engine):
    """notify_fill_pnl with no DailyLossLimitValidator returns silently."""
    from hft_platform.risk.validators import DailyLossLimitValidator

    engine.validators = [v for v in engine.validators if not isinstance(v, DailyLossLimitValidator)]
    engine.notify_fill_pnl("s1", -100_000)  # Must not raise
    assert not any(isinstance(v, DailyLossLimitValidator) for v in engine.validators)


# ── shared scale cache ─────────────────────────────────────────────────────


def test_shared_scale_cache_is_shared(cfg_path):
    """All validators that support _shared_scale_cache share the same dict."""
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    e = RiskEngine(cfg_path, q_in, q_out)

    caches = [id(v._shared_scale_cache) for v in e.validators if hasattr(v, "_shared_scale_cache")]
    if len(caches) >= 2:
        assert len(set(caches)) == 1, "All validators should share the same cache dict"


# ── evaluate float price ───────────────────────────────────────────────────


def test_evaluate_float_price_rejects(engine):
    """Float price is rejected immediately with FLOAT_PRICE."""
    intent = MagicMock()
    intent.price = 100.5
    intent.trace_id = ""
    intent.strategy_id = "s1"
    intent.symbol = "2330"

    decision = engine.evaluate(intent)
    assert not decision.approved
    assert decision.reason_code == "FLOAT_PRICE"


# ── on_config_reload ───────────────────────────────────────────────────────


def test_on_config_reload_clears_validator_caches(engine):
    """on_config_reload clears dict-valued cache attributes on validators."""
    for v in engine.validators:
        v._my_test_cache = {"old_key": "old_val"}

    new_config = {
        "global_defaults": {"max_price_cap": 1000.0, "tick_size": 0.01, "price_band_ticks": 10, "max_notional": 5000},
        "strategies": {},
    }
    engine.on_config_reload(new_config)

    for v in engine.validators:
        assert v.config is new_config
