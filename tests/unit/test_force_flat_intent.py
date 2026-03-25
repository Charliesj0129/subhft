"""Tests for FORCE_FLAT intent handling in StormGuard HALT/STORM and PlatformDegrade."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, StormGuardState
from hft_platform.ops.platform_degrade import PlatformDegradeController


def _make_intent(intent_type: IntentType) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="test",
        symbol="2330",
        intent_type=intent_type,
        side=Side.SELL,
        price=5000000,
        qty=1,
        timestamp_ns=0,
    )


class TestStormGuardForceFlat:
    """StormGuard.validate must allow FORCE_FLAT in HALT and STORM states."""

    def test_halt_allows_force_flat(self) -> None:
        from hft_platform.risk.storm_guard import StormGuard

        sg = StormGuard()
        sg.state = StormGuardState.HALT
        intent = _make_intent(IntentType.FORCE_FLAT)
        allowed, reason = sg.validate(intent)
        assert allowed is True
        assert reason == "OK"

    def test_halt_allows_cancel(self) -> None:
        from hft_platform.risk.storm_guard import StormGuard

        sg = StormGuard()
        sg.state = StormGuardState.HALT
        intent = _make_intent(IntentType.CANCEL)
        allowed, reason = sg.validate(intent)
        assert allowed is True

    def test_halt_blocks_new(self) -> None:
        from hft_platform.risk.storm_guard import StormGuard

        sg = StormGuard()
        sg.state = StormGuardState.HALT
        intent = _make_intent(IntentType.NEW)
        allowed, reason = sg.validate(intent)
        assert allowed is False
        assert reason == "STORMGUARD_HALT"

    def test_storm_allows_force_flat(self) -> None:
        from hft_platform.risk.storm_guard import StormGuard

        sg = StormGuard()
        sg.state = StormGuardState.STORM
        intent = _make_intent(IntentType.FORCE_FLAT)
        allowed, reason = sg.validate(intent)
        assert allowed is True
        assert reason == "OK"

    def test_storm_blocks_new(self) -> None:
        from hft_platform.risk.storm_guard import StormGuard

        sg = StormGuard()
        sg.state = StormGuardState.STORM
        intent = _make_intent(IntentType.NEW)
        allowed, reason = sg.validate(intent)
        assert allowed is False
        assert reason == "STORMGUARD_STORM_NEW_BLOCKED"


class TestPlatformDegradeForceFlat:
    """PlatformDegradeController.allow_intent must allow FORCE_FLAT in reduce-only."""

    def test_reduce_only_allows_force_flat(self) -> None:
        ctrl = PlatformDegradeController(metrics=None, evidence_writer=None)
        ctrl.enter_reduce_only(reason="test")
        assert ctrl.allow_intent(intent_type=IntentType.FORCE_FLAT, opens_risk=True) is True

    def test_reduce_only_blocks_new_opening(self) -> None:
        ctrl = PlatformDegradeController(metrics=None, evidence_writer=None)
        ctrl.enter_reduce_only(reason="test")
        assert ctrl.allow_intent(intent_type=IntentType.NEW, opens_risk=True) is False
