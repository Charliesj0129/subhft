"""Regression tests for the daily-loss chain found live on 2026-08-21.

Three defects, one chain, all reachable only once fills started recording again:

1. ``DailyLossLimitValidator`` converted NTD thresholds with
   ``price_scale // point_value``, but ``PositionStore`` / ``MtMCalculator``
   have already multiplied the price delta by the same point value. Every
   threshold was therefore enforced at 1/point_value of its configured size:
   in production a ``hard_limit_ntd: 5000`` halted at 670 NTD of realized loss
   and ``soft_limit_ntd: 500`` fired at 50 NTD.
2. ``StormGuard.update()`` recomputes its target state from drawdown, latency
   and feed gap only, so it cannot see a latched daily-loss stop. A clean tick
   de-escalated HALT and the still-latched validator flag re-halted it: 26
   escalations / 25 de-escalations in 27 minutes.
3. Each de-escalation restarted ``order`` and ``exec_gateway`` through the
   crash-recovery path, exhausting ``max_attempts=10`` and latching a HALT
   reading "crash-loop" with nothing having crashed.
"""

from __future__ import annotations

import os
from unittest import mock
from unittest.mock import MagicMock

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.execution.positions import Position
from hft_platform.risk.storm_guard import StormGuard, StormGuardState
from hft_platform.risk.validators import DailyLossLimitValidator

# One NTD in PnL-accumulator units. The producers have already applied the
# contract multiplier, so this is ``price_scale`` and nothing else.
NTD = 10_000


def _ntd(amount_ntd: int) -> int:
    return amount_ntd * NTD


def _storm_guard() -> StormGuard:
    """StormGuard with the HALT cooldown collapsed so de-escalation is reachable.

    Without this the tests would pass for the wrong reason: HALT would persist
    because of the 60 s cooldown, not because of the hold under test.
    """
    with mock.patch.dict(
        os.environ,
        {"HFT_STORMGUARD_HALT_COOLDOWN_S": "0", "HFT_STORMGUARD_DE_ESCALATE_N": "1"},
    ):
        sg = StormGuard()
    sg._halt_cooldown_s = 0.0
    sg._storm_cooldown_s = 0.0
    sg._warm_cooldown_s = 0.0
    sg._de_escalate_threshold = 1
    return sg


def _make_intent(strategy_id: str = "R47", symbol: str = "TMFI6") -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=452_130_000,
        qty=1,
    )


def _make_validator(point_value: int = 10, **overrides: object) -> DailyLossLimitValidator:
    """Validator carrying the production thresholds from config/env/prod."""
    intraday_pnl: dict[str, object] = {
        "scope": "global",
        "soft_limit_ntd": 500,
        "hard_limit_ntd": 5000,
        "peak_drawdown_pct": 0.40,
        "soft_recovery_ntd": 300,
        "drawdown_recovery_pct": 0.20,
        "soft_limit_cooldown_s": 60,
        "peak_drawdown_min_peak_ntd": 3500,
        "price_scale": 10000,
        "point_value": point_value,
    }
    intraday_pnl.update(overrides)
    return DailyLossLimitValidator(
        {"global_defaults": {"max_daily_loss": 500_000_000}, "intraday_pnl": intraday_pnl}, None
    )


# ---------------------------------------------------------------------------
# 1. Threshold scale
# ---------------------------------------------------------------------------


class TestDailyLossThresholdScale:
    def test_hard_limit_holds_just_under_the_configured_ntd_amount(self):
        v = _make_validator()
        v.record_pnl("R47", _ntd(-4999))
        ok, _ = v.check(_make_intent())
        # Rejected by SOFT_LIMIT (correctly), but the hard halt must not latch.
        assert v.halt_triggered is False
        assert ok is False

    def test_hard_limit_halts_at_the_configured_ntd_amount(self):
        v = _make_validator()
        v.record_pnl("R47", _ntd(-5000))
        v.check(_make_intent())
        assert v.halt_triggered is True

    def test_hard_limit_does_not_halt_at_one_tenth_of_the_configured_amount(self):
        """The exact production symptom: 5,000 NTD configured, halted at 670."""
        v = _make_validator()
        v.record_pnl("R47", _ntd(-670))
        v.check(_make_intent())
        assert v.halt_triggered is False

    def test_soft_limit_holds_just_under_the_configured_ntd_amount(self):
        v = _make_validator()
        v.record_pnl("R47", _ntd(-499))
        ok, _ = v.check(_make_intent())
        assert ok is True
        assert v.soft_limit_active is False

    def test_soft_limit_rejects_at_the_configured_ntd_amount(self):
        v = _make_validator()
        v.record_pnl("R47", _ntd(-500))
        ok, reason = v.check(_make_intent())
        assert ok is False
        assert "SOFT_LIMIT" in reason

    def test_soft_limit_does_not_fire_at_one_tenth_of_the_configured_amount(self):
        """The exact production symptom: 500 NTD configured, fired at 50."""
        v = _make_validator()
        v.record_pnl("R47", _ntd(-50))
        ok, _ = v.check(_make_intent())
        assert ok is True
        assert v.soft_limit_active is False

    def test_thresholds_are_independent_of_configured_point_value(self):
        """The producers already applied the point value; applying it again here
        made the error factor the point value itself -- 10x TMF, 200x TXF."""
        tmf = _make_validator(point_value=10)
        txf = _make_validator(point_value=200)
        assert tmf._hard_limit_threshold_scaled == txf._hard_limit_threshold_scaled == _ntd(5000)
        assert tmf._soft_limit_threshold_scaled == txf._soft_limit_threshold_scaled == _ntd(500)
        assert tmf._soft_recovery_threshold_scaled == txf._soft_recovery_threshold_scaled == _ntd(300)
        assert tmf._peak_drawdown_min_peak_scaled == txf._peak_drawdown_min_peak_scaled == _ntd(3500)

    def test_unrealized_hard_limit_uses_the_same_ntd_scale(self):
        v = _make_validator()
        v.update_unrealized(_ntd(-4999))
        assert v.halt_triggered is False
        v.update_unrealized(_ntd(-5000))
        assert v.halt_triggered is True


class TestProducerConsumerUnitAgreement:
    """The bug was a disagreement between two layers, so pin both ends of it.

    Reproduces the production fill pair that first exposed it: BUY 1 TMFI6 at
    45213, SELL at 45208, TMF point value 10 -> 5 adverse points = 50 NTD, and
    the validator logged ``total_pnl: -500000``.
    """

    @staticmethod
    def _fill(side: Side, price_scaled: int) -> FillEvent:
        return FillEvent(
            fill_id="f1",
            account_id="TEST",
            order_id="o1",
            strategy_id="R47",
            symbol="TMFI6",
            side=side,
            qty=1,
            price=price_scaled,
            fee=0,
            tax=0,
            ingest_ts_ns=0,
            match_ts_ns=0,
        )

    def test_a_five_point_tmf_loss_is_fifty_ntd_to_the_validator(self):
        pos = Position(account_id="TEST", strategy_id="R47", symbol="TMFI6")
        pos.update(self._fill(Side.BUY, 452_130_000), contract_multiplier=10)
        pos.update(self._fill(Side.SELL, 452_080_000), contract_multiplier=10)

        assert pos.realized_pnl_scaled == -500_000
        assert pos.realized_pnl_scaled == _ntd(-50)

        v = _make_validator()
        v.record_pnl("R47", pos.realized_pnl_scaled)
        ok, _ = v.check(_make_intent())
        # 50 NTD is nowhere near the 500 NTD soft limit.
        assert ok is True
        assert v.soft_limit_active is False
        assert v.halt_triggered is False

    def test_the_same_loss_on_a_txf_contract_is_twenty_times_larger(self):
        pos = Position(account_id="TEST", strategy_id="R47", symbol="TXFI6")
        pos.update(self._fill(Side.BUY, 452_130_000), contract_multiplier=200)
        pos.update(self._fill(Side.SELL, 452_080_000), contract_multiplier=200)

        assert pos.realized_pnl_scaled == _ntd(-1000)

        v = _make_validator()
        v.record_pnl("R47", pos.realized_pnl_scaled)
        ok, reason = v.check(_make_intent())
        assert ok is False
        assert "SOFT_LIMIT" in reason
        assert v.halt_triggered is False


# ---------------------------------------------------------------------------
# 2. HALT stays latched
# ---------------------------------------------------------------------------


def _clean_tick(sg: StormGuard) -> StormGuardState:
    """A tick with nothing wrong in the market: no drawdown, no lag, no gap."""
    return sg.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)


class TestDailyLossHoldBlocksDeEscalation:
    def test_halt_does_not_de_escalate_while_the_daily_loss_hold_is_set(self):
        sg = _storm_guard()
        sg.set_daily_loss_hold(True)
        sg.trigger_halt("DAILY_LOSS_LIMIT_EXCEEDED")

        # More clean ticks than the de-escalation threshold could ever need.
        for _ in range(20):
            _clean_tick(sg)

        assert sg.state == StormGuardState.HALT
        assert sg.daily_loss_hold is True

    def test_clearing_the_daily_loss_hold_allows_de_escalation(self):
        sg = _storm_guard()
        sg.set_daily_loss_hold(True)
        sg.trigger_halt("DAILY_LOSS_LIMIT_EXCEEDED")
        for _ in range(5):
            _clean_tick(sg)
        assert sg.state == StormGuardState.HALT

        sg.set_daily_loss_hold(False)
        for _ in range(5):
            _clean_tick(sg)
        assert sg.state < StormGuardState.HALT

    def test_the_hold_defaults_off_so_ordinary_halts_still_recover(self):
        sg = _storm_guard()
        assert sg.daily_loss_hold is False
        sg.trigger_halt("Critical Component Crash: OrderAdapter")
        for _ in range(5):
            _clean_tick(sg)
        assert sg.state < StormGuardState.HALT


class TestRiskEngineKeepsTheHoldInSync:
    @staticmethod
    def _engine(validator: DailyLossLimitValidator) -> object:
        from hft_platform.risk.engine import RiskEngine

        engine = RiskEngine.__new__(RiskEngine)
        engine.validators = [validator]
        engine.storm_guard = _storm_guard()
        engine._notification_dispatcher = None
        return engine

    def test_a_latched_validator_produces_a_halt_that_survives_clean_ticks(self):
        """End-to-end reproduction of the 65-second square wave."""
        v = _make_validator()
        engine = self._engine(v)

        v.record_pnl("R47", _ntd(-6000))
        v.check(_make_intent())
        assert v.halt_triggered is True

        engine._check_daily_loss_halt()
        assert engine.storm_guard.state == StormGuardState.HALT
        assert engine.storm_guard.daily_loss_hold is True

        for _ in range(30):
            _clean_tick(engine.storm_guard)
            engine._check_daily_loss_halt()

        assert engine.storm_guard.state == StormGuardState.HALT

    def test_the_daily_reset_releases_the_hold(self):
        v = _make_validator()
        engine = self._engine(v)

        v.record_pnl("R47", _ntd(-6000))
        v.check(_make_intent())
        engine._check_daily_loss_halt()
        assert engine.storm_guard.daily_loss_hold is True

        v._force_reset()
        engine._check_daily_loss_halt()
        assert engine.storm_guard.daily_loss_hold is False

        for _ in range(5):
            _clean_tick(engine.storm_guard)
        assert engine.storm_guard.state < StormGuardState.HALT

    def test_sync_runs_even_while_already_halted(self):
        """The short circuit for 'already in HALT' must not skip the release."""
        v = _make_validator()
        engine = self._engine(v)
        engine.storm_guard.trigger_halt("something else")
        engine.storm_guard.set_daily_loss_hold(True)

        assert v.halt_triggered is False
        engine._check_daily_loss_halt()
        assert engine.storm_guard.daily_loss_hold is False

    def test_sync_is_a_no_op_without_a_daily_loss_validator(self):
        from hft_platform.risk.engine import RiskEngine

        engine = RiskEngine.__new__(RiskEngine)
        engine.validators = [MagicMock()]
        engine.storm_guard = _storm_guard()
        engine._notification_dispatcher = None

        engine._check_daily_loss_halt()
        assert engine.storm_guard.daily_loss_hold is False
