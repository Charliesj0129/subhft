"""B5: the configured limits are the enforced ones, and every stop leaves a record.

Before this batch:

- the notional caps compared ``price * qty`` with no point value against a prod
  value already multiplied by 10,000 and then multiplied again: a cap of 10^13
  NTD that no order could reach;
- ``max_order_qty``, ``halt_requires_manual_recovery`` and four more prod keys,
  and all of ``config/env/prod/risk.yaml``, were read by nothing;
- nothing logged which thresholds were in force;
- ``stormguard_mode`` said HALT but not why;
- platform reduce-only refusals (about 965 on 2026-10-21) reached no audit table;
- a restart reset StormGuard to NORMAL without a guardrail_log row.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from prometheus_client import CollectorRegistry, Gauge

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side, StormGuardState
from hft_platform.core import timebase
from hft_platform.core.pricing import SymbolMetadataPriceScaleProvider
from hft_platform.risk import storm_guard as storm_guard_mod
from hft_platform.risk.engine import RiskEngine
from hft_platform.risk.storm_guard import StormGuard, halt_reason_label
from hft_platform.risk.validators import MaxNotionalValidator, PerSymbolNotionalValidator

_REPO = Path(__file__).resolve().parents[2]
_PROD_LIMITS = _REPO / "config" / "env" / "prod" / "strategy_limits.yaml"
_SCALE = 10_000
_POINT_VALUE = {"TMFK6": 10, "TXFK6": 200}


class _Metadata:
    """The two SymbolMetadata methods the validators read."""

    def price_scale(self, symbol: str) -> int:
        return _SCALE

    def contract_multiplier(self, symbol: str) -> int:
        return _POINT_VALUE.get(symbol, 1)


def _prod_config() -> dict:
    return yaml.safe_load(_PROD_LIMITS.read_text(encoding="utf-8"))


def _intent(symbol: str, *, points: int = 45_000, side: Side = Side.BUY, qty: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="R47_MAKER_TMF",
        symbol=symbol,
        intent_type=IntentType.NEW,
        side=side,
        price=points * _SCALE,
        qty=qty,
    )


def _notional_validators(position: int = 0) -> list:
    provider = SymbolMetadataPriceScaleProvider(_Metadata())
    cfg = _prod_config()
    return [
        cls(cfg, provider, position_provider=lambda symbol, strategy_id: position)
        for cls in (MaxNotionalValidator, PerSymbolNotionalValidator)
    ]


class TestNotionalCapIsNtd:
    def test_one_tmf_lot_passes_the_prod_cap(self) -> None:
        """45,000 points x 10 NTD = 450,000 NTD, under 1,000,000."""
        for v in _notional_validators():
            ok, reason = v.check(_intent("TMFK6"))
            assert ok is True, (type(v).__name__, reason)

    def test_one_txf_lot_is_rejected_by_the_prod_cap(self) -> None:
        """45,000 points x 200 NTD = 9,000,000 NTD; before B5 this passed."""
        for v in _notional_validators():
            ok, reason = v.check(_intent("TXFK6"))
            assert ok is False, type(v).__name__
            assert "NOTIONAL_EXCEEDED" in reason

    def test_a_txf_order_that_reduces_a_position_is_exempt(self) -> None:
        for v in _notional_validators(position=1):
            ok, reason = v.check(_intent("TXFK6", side=Side.SELL))
            assert ok is True, (type(v).__name__, reason)

    def test_three_tmf_lots_cross_the_cap(self) -> None:
        """1,350,000 NTD: the qty is in the product too."""
        for v in _notional_validators():
            assert v.check(_intent("TMFK6", qty=3))[0] is False, type(v).__name__

    def test_without_metadata_the_point_value_is_one(self) -> None:
        """Stocks and tests without SymbolMetadata keep the old arithmetic."""
        cfg = {"global_defaults": {"max_notional": 100_000, "per_symbol_max_notional": 100_000}}
        provider = MagicMock(spec=["price_scale"])
        provider.price_scale.return_value = _SCALE
        for cls in (MaxNotionalValidator, PerSymbolNotionalValidator):
            v = cls(cfg, provider)
            assert v.check(_intent("2330", points=99_999))[0] is True
            assert v.check(_intent("2330", points=100_001))[0] is False


class TestRustDoesNotJudgeNotional:
    """HFT_RISK_RUST_VALIDATOR=1: the kernel has no reduce exemption and returns first."""

    @pytest.fixture
    def rust_engine(self, monkeypatch: pytest.MonkeyPatch):
        from hft_platform.risk.engine import _load_rust_risk_validator

        if _load_rust_risk_validator() is None:
            pytest.skip("rust_core RustRiskValidator not built")
        monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "1")

        def build(position: int) -> RiskEngine:
            provider = SymbolMetadataPriceScaleProvider(_Metadata())
            with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
                sg = StormGuard()
            engine = RiskEngine(
                str(_PROD_LIMITS),
                asyncio.Queue(),
                asyncio.Queue(),
                provider,
                storm_guard=sg,
                position_provider=lambda symbol, strategy_id: position,
            )
            assert engine._rust_validator is not None
            return engine

        return build

    def test_an_over_cap_position_can_still_be_closed(self, rust_engine) -> None:
        """23 TMF at 45,000 = 10,350,000 NTD. Rust used to reject it as MAX_NOTIONAL."""
        engine = rust_engine(position=23)
        decision = engine.evaluate(_intent("TMFK6", side=Side.SELL, qty=23))
        assert decision.approved is True, decision.reason_code

    def test_an_opening_txf_lot_is_still_rejected_by_python(self, rust_engine) -> None:
        engine = rust_engine(position=0)
        decision = engine.evaluate(_intent("TXFK6"))
        assert decision.approved is False
        assert "NOTIONAL_EXCEEDED" in decision.reason_code


def _load_dead_config_scan():
    path = _REPO / "scripts" / "dead_config_scan.py"
    spec = importlib.util.spec_from_file_location("dead_config_scan_b5", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestProdLimitsHoldOnlyReadKeys:
    def test_every_prod_strategy_limits_key_is_read(self) -> None:
        scan = _load_dead_config_scan()
        dead = scan.find_dead_keys(_REPO, "config/env/prod/strategy_limits.yaml")
        assert [f.detail for f in dead] == []

    @pytest.mark.parametrize("path", ["config/base/strategy_limits.yaml", "config/env/prod/strategy_limits.yaml"])
    def test_no_unread_order_size_or_mirror_keys(self, path: str) -> None:
        text = (_REPO / path).read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        assert "global_limits" not in data
        assert "max_order_qty:" not in text


class TestEffectiveThresholdsAreLogged:
    def _engine(self) -> RiskEngine:
        provider = SymbolMetadataPriceScaleProvider(_Metadata())
        with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
            sg = StormGuard()
        return RiskEngine(str(_PROD_LIMITS), asyncio.Queue(), asyncio.Queue(), provider, storm_guard=sg)

    def test_the_report_carries_the_prod_limits_in_ntd(self) -> None:
        report = self._engine().effective_thresholds()

        assert report["max_notional_ntd"] == 1_000_000
        assert report["per_symbol_max_notional_ntd"] == 1_000_000
        assert report["max_position_lots"] == 1
        assert report["daily_loss"]["soft_limit_ntd"] == 500
        assert report["daily_loss"]["hard_limit_ntd"] == 15_000
        assert report["daily_loss"]["position_loss_limit_ntd"] == 5_000
        assert report["loss_exit"]["enabled"] is True
        assert report["stormguard"]["order_rtt_storm_us"] > 0

    def test_boot_logs_it_once(self) -> None:
        with patch("hft_platform.risk.engine.logger") as log:
            self._engine()
        events = [c.args[0] for c in log.info.call_args_list if c.args]
        assert events.count("risk_thresholds_effective") == 1
        (call,) = [c for c in log.info.call_args_list if c.args and c.args[0] == "risk_thresholds_effective"]
        assert call.kwargs["trigger"] == "boot"
        assert call.kwargs["daily_loss"]["hard_limit_ntd"] == 15_000


class TestHaltReasonLabel:
    def _guard(self) -> tuple[StormGuard, Gauge]:
        with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
            sg = StormGuard()
        gauge = Gauge("stormguard_halt_reason", "test", ["reason"], registry=CollectorRegistry())
        sg.metrics.stormguard_halt_reason = gauge
        return sg, gauge

    @staticmethod
    def _value(gauge: Gauge, reason: str) -> float:
        return gauge.labels(reason=reason)._value.get()

    def test_halt_entry_sets_its_reason_and_leaving_clears_it(self) -> None:
        sg, gauge = self._guard()

        sg.trigger_halt("KILL_SWITCH_FILE: operator drill")
        assert self._value(gauge, "KILL_SWITCH_FILE") == 1

        with sg._state_lock:
            sg._transition(StormGuardState.NORMAL, "test release")
        sg._emit_pending_transition()
        assert self._value(gauge, "KILL_SWITCH_FILE") == 0

    def test_a_second_halt_moves_the_label(self) -> None:
        sg, gauge = self._guard()
        sg.trigger_halt("raw_queue_sustained_drops: 41 consecutive")
        with sg._state_lock:
            sg._transition(StormGuardState.STORM, "test")
        sg._emit_pending_transition()
        sg.trigger_halt("DAILY_LOSS_LIMIT_EXCEEDED")

        assert self._value(gauge, "raw_queue_sustained_drops") == 0
        assert self._value(gauge, "DAILY_LOSS_LIMIT_EXCEEDED") == 1

    def test_interleaved_halts_leave_no_label_set_after_release(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """trigger_halt runs on any thread; two HALT emits must not interleave.

        Thread A pauses inside its ``labels(CAUSE_A)`` call, before ``set(1)``.
        Thread B then moves HALT -> STORM -> HALT(CAUSE_B). Unserialized, B
        finishes, A resumes and sets CAUSE_A to 1 after B cleared it, and the
        release below clears only CAUSE_B.
        """
        sg, gauge = self._guard()
        real_labels = gauge.labels
        a_inside = threading.Event()
        release_a = threading.Event()

        def labels(**kw):
            if kw.get("reason") == "CAUSE_A" and not a_inside.is_set():
                a_inside.set()
                release_a.wait(timeout=5)
            return real_labels(**kw)

        monkeypatch.setattr(gauge, "labels", labels)

        def second_halt() -> None:
            with sg._state_lock:
                sg._transition(StormGuardState.STORM, "test")
            sg._emit_pending_transition()
            sg.trigger_halt("CAUSE_B")

        a = threading.Thread(target=sg.trigger_halt, args=("CAUSE_A",))
        a.start()
        assert a_inside.wait(timeout=5)
        b = threading.Thread(target=second_halt)
        b.start()
        # Without the lock B completes here; with it B waits on A. 50 ms bounds
        # the window B gets to finish before A is let go.
        b.join(timeout=0.05)
        release_a.set()
        a.join(timeout=5)
        b.join(timeout=5)
        assert not a.is_alive() and not b.is_alive()

        with sg._state_lock:
            sg._transition(StormGuardState.NORMAL, "test release")
        sg._emit_pending_transition()
        assert self._value(gauge, "CAUSE_A") == 0
        assert self._value(gauge, "CAUSE_B") == 0

    def test_detail_after_the_colon_is_not_a_label(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(storm_guard_mod, "_halt_reason_labels_seen", set())
        assert halt_reason_label("RECONCILIATION_MISMATCH: 2 critical discrepancies (['TMFK6'])") == (
            "RECONCILIATION_MISMATCH"
        )
        assert halt_reason_label("") == "unspecified"

    def test_label_cardinality_is_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(storm_guard_mod, "_halt_reason_labels_seen", set())
        cap = storm_guard_mod._HALT_REASON_LABEL_CAP
        labels = {halt_reason_label(f"cause_{i}") for i in range(cap + 10)}
        assert len(labels) == cap + 1
        assert "other" in labels


class TestLocalRejectsAreAudited:
    @pytest.fixture(autouse=True)
    def _mock_infra(self):
        with (
            patch("hft_platform.order.adapter.MetricsRegistry") as mm,
            patch("hft_platform.order.adapter.LatencyRecorder") as ml,
            patch("hft_platform.order.adapter.SymbolMetadata"),
            patch("hft_platform.order.adapter.PriceCodec"),
            patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
            patch("hft_platform.order.adapter.get_dlq") as md,
        ):
            mm.get.return_value = MagicMock()
            ml.get.return_value = MagicMock()
            md.return_value = MagicMock()
            yield

    @pytest.mark.asyncio
    async def test_a_platform_reduce_only_refusal_reaches_audit_risk_log(self, tmp_path) -> None:
        from hft_platform.order.adapter import OrderAdapter

        cfg = tmp_path / "order.yaml"
        cfg.write_text("rate_limits:\n  shioaji_soft_cap: 180\n  shioaji_hard_cap: 250\n  window_seconds: 10\n")
        client = MagicMock()
        client.get_exchange = MagicMock(return_value="TAIFEX")
        adapter = OrderAdapter(config_path=str(cfg), order_queue=asyncio.Queue(maxsize=8), broker_client=client)
        adapter._dlq = MagicMock()
        adapter._dlq.add = AsyncMock()
        audit = MagicMock()
        adapter.set_audit_writer(audit)
        adapter._platform_degrade_allows = MagicMock(return_value=False)
        intent = OrderIntent(
            intent_id=7,
            strategy_id="R47_MAKER_TMF",
            symbol="TMFK6",
            price=45_000 * _SCALE,
            qty=1,
            side=Side.BUY,
            intent_type=IntentType.NEW,
            tif=TIF.LIMIT,
        )
        cmd = OrderCommand(
            cmd_id=7,
            intent=intent,
            deadline_ns=timebase.now_ns() + 1_000_000_000,
            storm_guard_state=StormGuardState.NORMAL,
        )

        await adapter.execute(cmd)

        (row,) = [c.args[0] for c in audit.log_risk_decision.call_args_list]
        assert row["reason_code"] == "ADAPTER_PLATFORM_REDUCE_ONLY"
        assert row["approved"] is False
        assert row["symbol"] == "TMFK6"
        assert row["intent_type"] == int(IntentType.NEW)


class TestEngineStartIsAudited:
    def test_boot_writes_one_guardrail_row(self) -> None:
        from hft_platform.services.system import HFTSystem

        audit = MagicMock()
        sg = MagicMock()
        sg.state = StormGuardState.NORMAL
        with patch("hft_platform.recorder.audit.get_audit_writer", return_value=audit):
            HFTSystem._audit_engine_start(sg)

        audit.log_guardrail_transition.assert_called_once_with(
            {"old_state": "BOOT", "new_state": "NORMAL", "reason": "engine_start"}
        )

    def test_boot_row_precedes_the_latch_restores_in_run(self) -> None:
        """A HALT restored at boot must sort after the BOOT row, not before it."""
        import inspect

        from hft_platform.services.system import HFTSystem

        src = inspect.getsource(HFTSystem.run)
        assert src.index("_audit_engine_start") < src.index("kill_switch.is_active()")
        assert src.index("_audit_engine_start") < src.index("_restore_daily_loss_state()")


class TestEveryOperatorLatchHasAnAlert:
    def test_kill_switch_and_manual_latch_alerts_exist(self) -> None:
        rules = yaml.safe_load((_REPO / "config/monitoring/alerts/rules.yaml").read_text(encoding="utf-8"))
        exprs = {r["alert"]: r["expr"] for g in rules["groups"] for r in g["rules"] if "alert" in r}
        assert exprs["KillSwitchActive"] == "kill_switch_active == 1"
        assert exprs["ManualLatchActive"] == "manual_latch_active == 1"
        assert exprs["PlatformReduceOnlyActive"] == "platform_reduce_only_active == 1"
