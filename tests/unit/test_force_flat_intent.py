"""Tests for FORCE_FLAT IntentType across StormGuard and PlatformDegrade."""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, TIF
from hft_platform.ops.platform_degrade import PlatformDegradeController
from hft_platform.risk.storm_guard import StormGuard, StormGuardState


def _make_intent(intent_type: IntentType = IntentType.FORCE_FLAT) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="test",
        symbol="2330",
        intent_type=intent_type,
        side=Side.SELL,
        price=0,
        qty=1,
        tif=TIF.IOC,
    )


class TestForceFlatIntentType:
    def test_force_flat_enum_value(self) -> None:
        assert IntentType.FORCE_FLAT == 3
        assert IntentType.FORCE_FLAT.name == "FORCE_FLAT"

    def test_force_flat_coexists_with_existing_types(self) -> None:
        assert IntentType.NEW == 0
        assert IntentType.AMEND == 1
        assert IntentType.CANCEL == 2
        assert IntentType.FORCE_FLAT == 3
        assert len(IntentType) == 4


class TestStormGuardForceFlat:
    def test_force_flat_allowed_during_halt(self) -> None:
        sg = StormGuard()
        sg.transition(StormGuardState.HALT, "test")
        intent = _make_intent(IntentType.FORCE_FLAT)
        allowed, reason = sg.validate(intent)
        assert allowed is True
        assert reason == "OK"

    def test_new_blocked_during_halt(self) -> None:
        sg = StormGuard()
        sg.transition(StormGuardState.HALT, "test")
        intent = _make_intent(IntentType.NEW)
        allowed, _ = sg.validate(intent)
        assert allowed is False

    def test_cancel_still_allowed_during_halt(self) -> None:
        sg = StormGuard()
        sg.transition(StormGuardState.HALT, "test")
        intent = _make_intent(IntentType.CANCEL)
        allowed, reason = sg.validate(intent)
        assert allowed is True
        assert reason == "OK"


class TestPlatformDegradeForceFlat:
    def test_force_flat_allowed_during_reduce_only(self) -> None:
        ctrl = PlatformDegradeController(metrics=MagicMock())
        ctrl.enter_reduce_only(reason="test")
        result = ctrl.allow_intent(intent_type=IntentType.FORCE_FLAT, opens_risk=True)
        assert result is True

    def test_new_blocked_when_opens_risk_in_reduce_only(self) -> None:
        ctrl = PlatformDegradeController(metrics=MagicMock())
        ctrl.enter_reduce_only(reason="test")
        result = ctrl.allow_intent(intent_type=IntentType.NEW, opens_risk=True)
        assert result is False

    def test_cancel_allowed_in_reduce_only(self) -> None:
        ctrl = PlatformDegradeController(metrics=MagicMock())
        ctrl.enter_reduce_only(reason="test")
        result = ctrl.allow_intent(intent_type=IntentType.CANCEL, opens_risk=True)
        assert result is True
