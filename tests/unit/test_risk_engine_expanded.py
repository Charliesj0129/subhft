"""Expanded unit tests for RiskEngine."""

from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.contracts.strategy import (
    IntentType,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.risk.engine import RiskEngine
from tests.factories.intents import make_order_intent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_intent(
    *,
    price: int | float = 1000_0000,
    qty: int = 10,
    intent_type: IntentType = IntentType.NEW,
    strategy_id: str = "test_strat",
    symbol: str = "2330",
    side: Side = Side.BUY,
) -> OrderIntent:
    return make_order_intent(
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
    )


def _write_config(path: str, extra: dict | None = None) -> str:
    cfg: dict[str, Any] = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "tick_size": 0.01,
            "price_band_ticks": 20,
            "max_notional": 10_000_000,
            "max_qty": 1_000_000,
            "max_daily_loss": 500_000_000,
        },
        "strategies": {},
        "storm_guard": {
            "warm_threshold": -200_000,
            "storm_threshold": -500_000,
            "halt_threshold": -1_000_000,
        },
    }
    if extra:
        cfg.update(extra)
    filepath = os.path.join(path, "strategy_limits.yaml")
    with open(filepath, "w") as f:
        yaml.safe_dump(cfg, f)
    return filepath


@pytest.fixture(autouse=True)
def _disable_rust_and_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "0")
    monkeypatch.setenv("HFT_RISK_FAST_GATE", "0")
    monkeypatch.setenv("HFT_RISK_CMD_ID_LOCK", "0")


@pytest.fixture()
def _mock_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hft_platform.observability.metrics.MetricsRegistry.get", staticmethod(lambda: None))
    monkeypatch.setattr("hft_platform.observability.latency.LatencyRecorder.get", staticmethod(lambda: None))
    monkeypatch.setattr("hft_platform.recorder.audit.get_audit_writer", lambda: MagicMock())


@pytest.fixture()
def engine(tmp_path: Any, _mock_singletons: None) -> RiskEngine:
    cfg_path = _write_config(str(tmp_path))
    iq: asyncio.Queue = asyncio.Queue()
    oq: asyncio.Queue = asyncio.Queue()
    return RiskEngine(cfg_path, iq, oq)


# ---------------------------------------------------------------------------
# evaluate — float price rejection
# ---------------------------------------------------------------------------


class TestEvaluateFloatPrice:
    def test_float_price_rejected(self, engine: RiskEngine) -> None:
        intent = _make_intent(price=100.5)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "FLOAT_PRICE"


# ---------------------------------------------------------------------------
# evaluate — int price approval
# ---------------------------------------------------------------------------


class TestEvaluateIntPrice:
    def test_int_price_approved(self, engine: RiskEngine) -> None:
        intent = _make_intent(price=1000_0000)
        decision = engine.evaluate(intent)
        assert decision.approved
        assert decision.reason_code == "OK"


# ---------------------------------------------------------------------------
# evaluate — FastGate rejection / error / cancel-bypass
# ---------------------------------------------------------------------------


class TestEvaluateFastGate:
    def test_fast_gate_rejects(self, engine: RiskEngine) -> None:
        fake_gate = MagicMock()
        fake_gate.check.return_value = (False, 2)
        engine._fast_gate = fake_gate

        intent = _make_intent()
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert "FASTGATE" in decision.reason_code

    def test_fast_gate_error_fail_closed(self, engine: RiskEngine) -> None:
        fake_gate = MagicMock()
        fake_gate.check.side_effect = RuntimeError("boom")
        engine._fast_gate = fake_gate

        intent = _make_intent()
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "FASTGATE_ERROR"

    def test_fast_gate_bypassed_for_cancel(self, engine: RiskEngine) -> None:
        fake_gate = MagicMock()
        fake_gate.check.return_value = (False, 1)
        engine._fast_gate = fake_gate

        intent = _make_intent(intent_type=IntentType.CANCEL)
        decision = engine.evaluate(intent)
        # Cancel should bypass fast gate — storm_guard / validators still run
        assert decision.approved or "FASTGATE" not in decision.reason_code


# ---------------------------------------------------------------------------
# evaluate — StormGuard HALT
# ---------------------------------------------------------------------------


class TestEvaluateStormGuard:
    def test_halt_rejects_new(self, engine: RiskEngine) -> None:
        engine.storm_guard.state = StormGuardState.HALT
        intent = _make_intent()
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert "HALT" in decision.reason_code

    def test_halt_allows_cancel(self, engine: RiskEngine) -> None:
        engine.storm_guard.state = StormGuardState.HALT
        intent = _make_intent(intent_type=IntentType.CANCEL)
        decision = engine.evaluate(intent)
        assert decision.approved


# ---------------------------------------------------------------------------
# evaluate — Rust validator path
# ---------------------------------------------------------------------------


class TestEvaluateRustValidator:
    def test_rust_validator_rejects(self, engine: RiskEngine) -> None:
        rv = MagicMock()
        rv.check.return_value = (False, 1)
        engine._rust_validator = rv

        intent = _make_intent()
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "PRICE_ZERO_OR_NEG"

    def test_rust_validator_error_fail_closed(self, engine: RiskEngine) -> None:
        rv = MagicMock()
        rv.check.side_effect = RuntimeError("rust panic")
        engine._rust_validator = rv

        intent = _make_intent()
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "RUST_VALIDATOR_ERROR"


# ---------------------------------------------------------------------------
# evaluate — Python validators
# ---------------------------------------------------------------------------


class TestEvaluatePythonValidators:
    def test_python_validator_rejects(self, engine: RiskEngine) -> None:
        engine._rust_validator = None
        # Force first validator to reject
        engine.validators[0].check = MagicMock(return_value=(False, "PRICE_ZERO_OR_NEG"))
        intent = _make_intent()
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "PRICE_ZERO_OR_NEG"

    def test_all_python_validators_pass(self, engine: RiskEngine) -> None:
        engine._rust_validator = None
        for v in engine.validators:
            v.check = MagicMock(return_value=(True, "OK"))
        intent = _make_intent()
        decision = engine.evaluate(intent)
        assert decision.approved


# ---------------------------------------------------------------------------
# create_command
# ---------------------------------------------------------------------------


class TestCreateCommand:
    def test_deadline_is_in_future(self, engine: RiskEngine) -> None:
        from hft_platform.core import timebase

        intent = _make_intent()
        now = timebase.now_ns()
        cmd = engine.create_command(intent)
        assert cmd.deadline_ns > now
        assert cmd.deadline_ns - now <= 600_000_000  # within ~600ms

    def test_cmd_id_monotonic(self, engine: RiskEngine) -> None:
        intent = _make_intent()
        ids = [engine.create_command(intent).cmd_id for _ in range(5)]
        assert ids == sorted(ids)
        assert len(set(ids)) == 5

    def test_storm_guard_state_propagated(self, engine: RiskEngine) -> None:
        engine.storm_guard.state = StormGuardState.WARM
        intent = _make_intent()
        cmd = engine.create_command(intent)
        assert cmd.storm_guard_state == StormGuardState.WARM


# ---------------------------------------------------------------------------
# reload_config / on_config_reload
# ---------------------------------------------------------------------------


class TestReloadConfig:
    def test_reload_clears_caches(self, engine: RiskEngine, tmp_path: Any) -> None:
        # Populate a cache on a validator
        for v in engine.validators:
            if hasattr(v, "_max_price_scaled_cache"):
                v._max_price_scaled_cache["test"] = 999
                break

        new_cfg = {
            "global_defaults": {
                "max_price_cap": 9999.0,
                "tick_size": 0.01,
                "price_band_ticks": 20,
                "max_notional": 10_000_000,
                "max_daily_loss": 500_000_000,
            },
            "strategies": {},
            "storm_guard": {},
        }
        engine.on_config_reload(new_cfg)

        for v in engine.validators:
            if hasattr(v, "_max_price_scaled_cache"):
                assert len(v._max_price_scaled_cache) == 0

    def test_config_ref_updated(self, engine: RiskEngine) -> None:
        new_cfg = {
            "global_defaults": {"max_price_cap": 1234.0},
            "strategies": {"s1": {}},
        }
        engine.on_config_reload(new_cfg)
        assert engine.config is new_cfg
        for v in engine.validators:
            assert v.config is new_cfg

    def test_reload_config_reads_file(self, engine: RiskEngine, tmp_path: Any) -> None:
        # Rewrite the config file with changed value
        new_cfg = {
            "global_defaults": {
                "max_price_cap": 7777.0,
                "tick_size": 0.01,
                "price_band_ticks": 20,
                "max_notional": 10_000_000,
                "max_daily_loss": 500_000_000,
            },
            "strategies": {},
            "storm_guard": {},
        }
        with open(engine.config_path, "w") as f:
            yaml.safe_dump(new_cfg, f)

        engine.reload_config()
        assert engine.config["global_defaults"]["max_price_cap"] == 7777.0


# ---------------------------------------------------------------------------
# notify_fill_pnl
# ---------------------------------------------------------------------------


class TestNotifyFillPnl:
    def test_routes_to_daily_loss_validator(self, engine: RiskEngine) -> None:
        from hft_platform.risk.validators import DailyLossLimitValidator

        for v in engine.validators:
            if isinstance(v, DailyLossLimitValidator):
                v.record_pnl = MagicMock()
                engine.notify_fill_pnl("s1", -100_000)
                v.record_pnl.assert_called_once_with("s1", -100_000)
                return
        pytest.fail("DailyLossLimitValidator not found")


# ---------------------------------------------------------------------------
# _bool_env
# ---------------------------------------------------------------------------


class TestBoolEnv:
    def test_none_returns_default(self) -> None:
        assert RiskEngine._bool_env(None, default=True) is True
        assert RiskEngine._bool_env(None, default=False) is False

    def test_bool_passthrough(self) -> None:
        assert RiskEngine._bool_env(True) is True
        assert RiskEngine._bool_env(False) is False

    def test_string_1(self) -> None:
        assert RiskEngine._bool_env("1") is True

    def test_string_0(self) -> None:
        assert RiskEngine._bool_env("0") is False

    def test_string_yes(self) -> None:
        assert RiskEngine._bool_env("yes") is True


# ---------------------------------------------------------------------------
# _parse_sample_every
# ---------------------------------------------------------------------------


class TestParseSampleEvery:
    def test_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_SAMPLE", "5")
        assert RiskEngine._parse_sample_every("TEST_SAMPLE", default=1) == 5

    def test_invalid_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_SAMPLE_BAD", "abc")
        assert RiskEngine._parse_sample_every("TEST_SAMPLE_BAD", default=3) == 3

    def test_zero_becomes_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_SAMPLE_ZERO", "0")
        assert RiskEngine._parse_sample_every("TEST_SAMPLE_ZERO", default=1) == 1


# ---------------------------------------------------------------------------
# _emit_reject_metric
# ---------------------------------------------------------------------------


class TestEmitRejectMetric:
    def test_sampling_skips(self, engine: RiskEngine) -> None:
        """When sample_every=2, first call emits, second skips."""
        fake_metrics = MagicMock()
        engine.metrics = fake_metrics
        engine._reject_metric_cache_owner_id = id(fake_metrics)
        engine._reject_metric_sample_every = 2
        engine._reject_metric_counter = 0

        engine._emit_reject_metric("s1", "REASON")
        # Counter wraps 0->1 on first call; emission happens only when counter==0
        # after increment: (0+1) % 2 = 1 != 0, so first call is skipped
        # second call: (1+1) % 2 = 0, so it emits
        engine._emit_reject_metric("s1", "REASON")
        # The labels().inc() should have been called once
        assert fake_metrics.risk_reject_total.labels.call_count == 1

    def test_metrics_none_noop(self, engine: RiskEngine) -> None:
        engine.metrics = None
        # Should not raise
        engine._emit_reject_metric("s1", "REASON")
        assert engine.metrics is None, "metrics should remain None"


# ---------------------------------------------------------------------------
# Thread-safe cmd_id with lock
# ---------------------------------------------------------------------------


class TestThreadSafeCmdId:
    def test_concurrent_cmd_ids_unique(
        self, tmp_path: Any, _mock_singletons: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_RISK_CMD_ID_LOCK", "1")
        cfg_path = _write_config(str(tmp_path))
        iq: asyncio.Queue = asyncio.Queue()
        oq: asyncio.Queue = asyncio.Queue()
        eng = RiskEngine(cfg_path, iq, oq)
        assert eng._cmd_id_lock is not None

        results: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            local_ids = [eng._next_cmd_id() for _ in range(100)]
            with lock:
                results.extend(local_ids)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 2000
        assert len(set(results)) == 2000  # all unique


# ---------------------------------------------------------------------------
# monotonic_cmd_id property
# ---------------------------------------------------------------------------


class TestMonotonicCmdIdProperty:
    def test_property_reads_current(self, engine: RiskEngine) -> None:
        assert engine.monotonic_cmd_id == 0
        engine._next_cmd_id()
        assert engine.monotonic_cmd_id == 1


# ---------------------------------------------------------------------------
# evaluate_typed_frame
# ---------------------------------------------------------------------------


class TestEvaluateTypedFrame:
    def test_delegates_to_evaluate(self, engine: RiskEngine) -> None:
        intent = _make_intent()
        engine.evaluate = MagicMock(return_value=SimpleNamespace(approved=True, reason_code="OK"))
        frame = SimpleNamespace(
            price=1000_0000, qty=1, intent_type=IntentType.NEW, strategy_id="s", symbol="2330", side=Side.BUY
        )

        with patch.object(engine, "typed_frame_view", return_value=frame):
            result = engine.evaluate_typed_frame(frame)
        assert result.approved
