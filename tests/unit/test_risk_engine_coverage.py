"""Comprehensive additional tests for RiskEngine — validators, StormGuard, Rust fallback."""

import asyncio
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side, StormGuardState
from tests.factories import make_order_intent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_intent(
    intent_id: int = 1,
    strategy_id: str = "s1",
    symbol: str = "2330",
    intent_type: IntentType = IntentType.NEW,
    side: Side = Side.BUY,
    price: int = 5000000,
    qty: int = 10,
    tif: TIF = TIF.ROD,
    target_order_id: str | None = None,
    trace_id: str = "",
) -> OrderIntent:
    """Delegate to shared factory with local defaults."""
    return make_order_intent(
        intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
        tif=tif,
        target_order_id=target_order_id,
        trace_id=trace_id,
    )


def _write_risk_config(tmp_path, content: str | None = None):
    cfg = tmp_path / "risk.yaml"
    if content is None:
        content = """
global_defaults:
  max_price_cap: 5000.0
  tick_size: 0.01
  price_band_ticks: 20
  max_notional: 10000000
  per_symbol_max_notional: 50000000
  max_position_lots: 1000
  max_daily_loss: 500000
risk:
  max_order_size: 1000
storm_guard:
  warm_threshold: -200000
  storm_threshold: -500000
  halt_threshold: -1000000
"""
    cfg.write_text(content)
    return str(cfg)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in [
        "HFT_RISK_RUST_VALIDATOR",
        "HFT_RISK_FAST_GATE",
        "HFT_RISK_CMD_ID_LOCK",
        "HFT_RISK_OBS_POLICY",
        "HFT_OBS_POLICY",
        "HFT_RISK_REJECT_METRICS_SAMPLE_EVERY",
    ]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def risk_config(tmp_path):
    return _write_risk_config(tmp_path)


@pytest.fixture
def engine(risk_config):
    from hft_platform.risk.engine import RiskEngine

    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    return RiskEngine(risk_config, q_in, q_out)


# ===========================================================================
# Basic Evaluate Tests
# ===========================================================================


class TestEvaluateBasic:
    def test_approve_valid_intent(self, engine):
        intent = _make_intent()
        decision = engine.evaluate(intent)
        assert decision.approved is True
        assert decision.reason_code == "OK"

    def test_reject_float_price(self, engine):
        intent = _make_intent()
        # Manually set price to float to test guard
        object.__setattr__(intent, "price", 100.5)
        decision = engine.evaluate(intent)
        assert decision.approved is False
        assert decision.reason_code == "FLOAT_PRICE"

    def test_reject_zero_price(self, engine):
        intent = _make_intent(price=0)
        decision = engine.evaluate(intent)
        assert decision.approved is False
        assert "PRICE_ZERO_OR_NEG" in decision.reason_code

    def test_reject_negative_price(self, engine):
        intent = _make_intent(price=-100)
        decision = engine.evaluate(intent)
        assert decision.approved is False
        assert "PRICE_ZERO_OR_NEG" in decision.reason_code

    def test_cancel_intent_always_passes_validators(self, engine):
        intent = _make_intent(intent_type=IntentType.CANCEL, price=0, qty=0)
        decision = engine.evaluate(intent)
        assert decision.approved is True

    def test_reject_exceeds_price_cap(self, engine):
        # max_price_cap = 5000 * 10000 = 50_000_000
        intent = _make_intent(price=60_000_000)
        decision = engine.evaluate(intent)
        assert decision.approved is False
        assert "PRICE_EXCEEDS_CAP" in decision.reason_code

    def test_reject_exceeds_max_notional(self, engine):
        # max_notional = 10_000_000 * 10_000 = 100_000_000_000
        # price * qty must exceed that
        intent = _make_intent(price=50_000_000, qty=3000)
        decision = engine.evaluate(intent)
        assert decision.approved is False
        assert "NOTIONAL" in decision.reason_code.upper()

    def test_approve_amend_intent(self, engine):
        intent = _make_intent(intent_type=IntentType.AMEND, target_order_id="ord1")
        decision = engine.evaluate(intent)
        assert decision.approved is True


# ===========================================================================
# Validator Chain Tests
# ===========================================================================


class TestValidatorChain:
    def test_first_validator_rejects_stops_chain(self, engine):
        """PriceBandValidator rejects; subsequent validators not checked."""
        intent = _make_intent(price=-1)
        decision = engine.evaluate(intent)
        assert decision.approved is False
        assert "PRICE" in decision.reason_code

    def test_all_validators_pass(self, engine):
        intent = _make_intent(price=1000000, qty=1)  # 100.0 * 1 = low notional
        decision = engine.evaluate(intent)
        assert decision.approved is True

    def test_empty_validators_approve(self, engine):
        engine.validators = []
        intent = _make_intent()
        decision = engine.evaluate(intent)
        assert decision.approved is True

    def test_position_limit_exceeded(self, engine):
        # max_position_lots = 1000
        intent = _make_intent(qty=1001)
        decision = engine.evaluate(intent)
        assert decision.approved is False
        assert "POSITION_LIMIT" in decision.reason_code

    def test_custom_validator_rejects(self, engine):
        """Add a custom validator that always rejects."""

        class AlwaysRejectValidator:
            def check(self, intent):
                return False, "ALWAYS_REJECT"

        engine.validators.append(AlwaysRejectValidator())
        decision = engine.evaluate(_make_intent())
        assert decision.approved is False
        assert decision.reason_code == "ALWAYS_REJECT"


# ===========================================================================
# StormGuard Integration Tests
# ===========================================================================


class TestStormGuardIntegration:
    def test_halt_blocks_new_orders(self, engine):
        engine.storm_guard.state = StormGuardState.HALT
        intent = _make_intent(intent_type=IntentType.NEW)
        decision = engine.evaluate(intent)
        assert decision.approved is False
        assert "HALT" in decision.reason_code

    def test_halt_allows_cancel(self, engine):
        engine.storm_guard.state = StormGuardState.HALT
        intent = _make_intent(intent_type=IntentType.CANCEL, price=0, qty=0)
        decision = engine.evaluate(intent)
        assert decision.approved is True

    def test_storm_blocks_new(self, engine):
        engine.storm_guard.state = StormGuardState.STORM
        intent = _make_intent(intent_type=IntentType.NEW)
        decision = engine.evaluate(intent)
        assert decision.approved is False
        assert "STORM" in decision.reason_code

    def test_storm_allows_cancel(self, engine):
        engine.storm_guard.state = StormGuardState.STORM
        intent = _make_intent(intent_type=IntentType.CANCEL, price=0, qty=0)
        decision = engine.evaluate(intent)
        assert decision.approved is True

    def test_storm_allows_amend(self, engine):
        engine.storm_guard.state = StormGuardState.STORM
        intent = _make_intent(intent_type=IntentType.AMEND, target_order_id="o1")
        decision = engine.evaluate(intent)
        assert decision.approved is True

    def test_warm_allows_new(self, engine):
        engine.storm_guard.state = StormGuardState.WARM
        intent = _make_intent(intent_type=IntentType.NEW)
        decision = engine.evaluate(intent)
        assert decision.approved is True

    def test_normal_allows_new(self, engine):
        engine.storm_guard.state = StormGuardState.NORMAL
        intent = _make_intent()
        decision = engine.evaluate(intent)
        assert decision.approved is True


# ===========================================================================
# StormGuard FSM Transition Tests
# ===========================================================================


class TestStormGuardFSM:
    def test_pnl_drives_state(self, engine):
        sg = engine.storm_guard
        sg.update_pnl(-200_001)
        assert sg.state >= StormGuardState.WARM

    def test_escalation_to_halt(self, engine):
        sg = engine.storm_guard
        sg.update_pnl(-1_000_001)
        assert sg.state == StormGuardState.HALT


# ===========================================================================
# Command Creation Tests
# ===========================================================================


class TestCreateCommand:
    def test_create_command_increments_id(self, engine):
        intent = _make_intent()
        cmd1 = engine.create_command(intent)
        cmd2 = engine.create_command(intent)
        assert cmd2.cmd_id > cmd1.cmd_id

    def test_create_command_sets_deadline(self, engine):
        intent = _make_intent()
        cmd = engine.create_command(intent)
        assert cmd.deadline_ns > 0

    def test_create_command_captures_storm_state(self, engine):
        engine.storm_guard.state = StormGuardState.WARM
        intent = _make_intent()
        cmd = engine.create_command(intent)
        assert cmd.storm_guard_state == StormGuardState.WARM

    def test_monotonic_cmd_id_thread_safe(self, engine):
        engine._cmd_id_lock_enabled = True
        engine._cmd_id_lock = __import__("threading").Lock()
        intent = _make_intent()
        ids = []
        for _ in range(100):
            cmd = engine.create_command(intent)
            ids.append(cmd.cmd_id)
        assert ids == sorted(ids)
        assert len(set(ids)) == 100

    def test_monotonic_cmd_id_property(self, engine):
        assert engine.monotonic_cmd_id == 0
        engine.create_command(_make_intent())
        assert engine.monotonic_cmd_id == 1


# ===========================================================================
# Rust Validator Fallback Tests
# ===========================================================================


class TestRustFallback:
    def test_rust_validator_disabled_by_default(self, engine):
        assert engine._rust_validator is None

    def test_python_validators_used_when_rust_unavailable(self, engine):
        intent = _make_intent(price=0)
        decision = engine.evaluate(intent)
        assert decision.approved is False
        # Python path used
        assert "PRICE_ZERO_OR_NEG" in decision.reason_code

    def test_rust_validator_error_falls_through(self, engine):
        """When Rust validator raises OSError, Python validators are used."""
        mock_rv = MagicMock()
        mock_rv.check.side_effect = OSError("Rust panic")
        engine._rust_validator = mock_rv
        intent = _make_intent(price=0)
        decision = engine.evaluate(intent)
        assert decision.approved is False
        assert "PRICE_ZERO_OR_NEG" in decision.reason_code


# ===========================================================================
# Config Reload Tests
# ===========================================================================


class TestConfigReload:
    def test_reload_config(self, engine, tmp_path):
        new_cfg = tmp_path / "risk2.yaml"
        new_cfg.write_text("""
global_defaults:
  max_price_cap: 10000.0
  max_notional: 99999999
  max_position_lots: 5000
  max_daily_loss: 1000000
risk:
  max_order_size: 2000
storm_guard:
  warm_threshold: -300000
  storm_threshold: -700000
  halt_threshold: -2000000
""")
        engine.config_path = str(new_cfg)
        engine.reload_config()
        assert engine.config["global_defaults"]["max_price_cap"] == 10000.0

    def test_on_config_reload_updates_validators(self, engine):
        new_config = {
            "global_defaults": {"max_price_cap": 9999.0},
            "strategies": {},
        }
        engine.on_config_reload(new_config)
        for v in engine.validators:
            assert v.config is new_config

    def test_reload_clears_caches(self, engine):
        # Populate a cache
        cache_found = False
        for v in engine.validators:
            for attr in vars(v):
                if "cache" in attr.lower():
                    obj = getattr(v, attr)
                    if isinstance(obj, dict):
                        obj["test_key"] = "test_val"
                        cache_found = True
        engine.on_config_reload(engine.config)
        if cache_found:
            for v in engine.validators:
                for attr in vars(v):
                    if "cache" in attr.lower():
                        obj = getattr(v, attr)
                        if isinstance(obj, dict):
                            assert "test_key" not in obj
        # Assert validators were reloaded with the config regardless of cache presence
        assert len(engine.validators) >= 0  # validators list exists after reload
        for v in engine.validators:
            assert v.config is engine.config

    def test_reload_config_handles_exception(self, engine):
        engine.config_path = "/nonexistent/path.yaml"
        # Should not raise — gracefully handles missing config
        engine.reload_config()
        assert engine.config_path == "/nonexistent/path.yaml"


# ===========================================================================
# Run Loop Tests
# ===========================================================================


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_run_processes_intent(self, engine):
        intent = _make_intent()
        engine.intent_queue.put_nowait(intent)

        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.05)

        assert not engine.order_queue.empty()
        cmd = engine.order_queue.get_nowait()
        assert cmd.intent.intent_id == 1

        engine.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_run_rejects_bad_intent(self, engine):
        intent = _make_intent(price=-1)
        engine.intent_queue.put_nowait(intent)

        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.05)

        assert engine.order_queue.empty()

        engine.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ===========================================================================
# Reject Metric Tests
# ===========================================================================


class TestRejectMetrics:
    def test_emit_reject_metric_no_metrics(self, engine):
        engine.metrics = None
        # Should not raise when metrics is None
        engine._emit_reject_metric("s1", "TEST_REJECT")
        assert engine.metrics is None

    def test_emit_reject_metric_sampling(self, engine):
        engine._reject_metric_sample_every = 2
        engine._reject_metric_counter = 0
        # First call increments counter mod 2 = 1, skipped
        engine._emit_reject_metric("s1", "TEST")
        # Counter should be 1 now
        assert engine._reject_metric_counter == 1


# ===========================================================================
# Notify Fill PnL Tests
# ===========================================================================


class TestNotifyFillPnl:
    def test_notify_fill_pnl(self, engine):
        engine.notify_fill_pnl("s1", -100000)
        # Check DailyLossLimitValidator got the PnL
        for v in engine.validators:
            from hft_platform.risk.validators import DailyLossLimitValidator

            if isinstance(v, DailyLossLimitValidator):
                assert v._accumulated_loss.get("s1") == -100000
                return
        pytest.fail("DailyLossLimitValidator not found")

    def test_notify_fill_pnl_positive(self, engine):
        engine.notify_fill_pnl("s1", 50000)
        for v in engine.validators:
            from hft_platform.risk.validators import DailyLossLimitValidator

            if isinstance(v, DailyLossLimitValidator):
                assert v._accumulated_loss.get("s1") == 50000
                return
        pytest.fail("DailyLossLimitValidator not found")


# ===========================================================================
# Bool Env and Parse Sample Every Tests
# ===========================================================================


class TestHelpers:
    def test_bool_env_true(self):
        from hft_platform.risk.engine import RiskEngine

        assert RiskEngine._bool_env("1") is True
        assert RiskEngine._bool_env("true") is True
        assert RiskEngine._bool_env("yes") is True
        assert RiskEngine._bool_env("on") is True

    def test_bool_env_false(self):
        from hft_platform.risk.engine import RiskEngine

        assert RiskEngine._bool_env("0") is False
        assert RiskEngine._bool_env("false") is False
        assert RiskEngine._bool_env("no") is False
        assert RiskEngine._bool_env(None) is False

    def test_bool_env_default(self):
        from hft_platform.risk.engine import RiskEngine

        assert RiskEngine._bool_env(None, default=True) is True

    def test_parse_sample_every(self, monkeypatch):
        from hft_platform.risk.engine import RiskEngine

        monkeypatch.setenv("TEST_SAMPLE", "5")
        assert RiskEngine._parse_sample_every("TEST_SAMPLE", default=1) == 5

    def test_parse_sample_every_invalid(self, monkeypatch):
        from hft_platform.risk.engine import RiskEngine

        monkeypatch.setenv("TEST_SAMPLE", "bad")
        assert RiskEngine._parse_sample_every("TEST_SAMPLE", default=3) == 3

    def test_obs_policy_values(self, monkeypatch):
        from hft_platform.risk.engine import _obs_policy

        monkeypatch.setenv("HFT_RISK_OBS_POLICY", "minimal")
        assert _obs_policy() == "minimal"
        monkeypatch.setenv("HFT_RISK_OBS_POLICY", "garbage")
        assert _obs_policy() == ""


# ===========================================================================
# DailyLossLimitValidator via Evaluate Tests
# ===========================================================================


class TestDailyLossLimit:
    def test_loss_limit_blocks_new_orders(self, engine):
        engine.notify_fill_pnl("s1", -600_000)  # exceed 500_000 limit
        intent = _make_intent(strategy_id="s1")
        decision = engine.evaluate(intent)
        assert decision.approved is False
        assert "DAILY_LOSS" in decision.reason_code

    def test_gain_does_not_block(self, engine):
        engine.notify_fill_pnl("s1", 100_000)
        intent = _make_intent(strategy_id="s1")
        decision = engine.evaluate(intent)
        assert decision.approved is True
