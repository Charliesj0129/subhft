"""StormGuardFSM validator parity tests.

Tests the StormGuardFSM class in risk/validators.py, covering:
- Drawdown-based state transitions (NORMAL -> WARM/STORM/HALT)
- HALT blocking NEW intents while allowing CANCEL
- Cooldown hysteresis preventing immediate de-escalation
- De-escalation counter requiring N consecutive periods below threshold
- Parametrized drawdown transition matrix
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType, Side, StormGuardState
from hft_platform.risk.validators import StormGuardFSM
from tests.conftest import make_order_intent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fsm(
    *,
    warm: int = -200_000,
    storm: int = -500_000,
    halt: int = -1_000_000,
    cooldown_s: float = 0.0,
    de_escalate_n: int = 1,
) -> StormGuardFSM:
    """Create a StormGuardFSM with controllable thresholds and hysteresis."""
    config: dict[str, Any] = {
        "storm_guard": {
            "warm_threshold": warm,
            "storm_threshold": storm,
            "halt_threshold": halt,
        },
    }
    with patch("hft_platform.risk.validators.MetricsRegistry") as mock_mr:
        mock_metrics = MagicMock()
        mock_mr.get.return_value = mock_metrics
        fsm = StormGuardFSM(config)
    fsm._storm_cooldown_s = cooldown_s
    fsm._de_escalate_threshold = de_escalate_n
    return fsm


# ---------------------------------------------------------------------------
# 1. Normal to Caution (WARM) boundary
# ---------------------------------------------------------------------------


class TestNormalToCautionBoundary:
    def test_just_above_warm_stays_normal(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-199_999)
        assert fsm.state == StormGuardState.NORMAL

    def test_at_warm_transitions(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-200_000)
        assert fsm.state == StormGuardState.WARM

    def test_below_warm_transitions(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-200_001)
        assert fsm.state == StormGuardState.WARM


# ---------------------------------------------------------------------------
# 2. Caution (WARM) to HALT boundary (via STORM)
# ---------------------------------------------------------------------------


class TestCautionToHaltBoundary:
    def test_at_storm_threshold(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

    def test_just_above_halt_stays_storm(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-999_999)
        assert fsm.state == StormGuardState.STORM

    def test_at_halt_threshold(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-1_000_000)
        assert fsm.state == StormGuardState.HALT

    def test_below_halt_threshold(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-1_500_000)
        assert fsm.state == StormGuardState.HALT


# ---------------------------------------------------------------------------
# 3. HALT blocks NEW orders
# ---------------------------------------------------------------------------


class TestHaltBlocksNewOrders:
    def test_halt_rejects_new_buy(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-1_000_000)
        assert fsm.state == StormGuardState.HALT

        intent = make_order_intent(intent_type=IntentType.NEW, side=Side.BUY)
        ok, reason = fsm.validate(intent)
        assert ok is False
        assert "HALT" in reason

    def test_halt_rejects_new_sell(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-1_000_000)

        intent = make_order_intent(intent_type=IntentType.NEW, side=Side.SELL)
        ok, reason = fsm.validate(intent)
        assert ok is False
        assert "HALT" in reason


# ---------------------------------------------------------------------------
# 4. HALT allows CANCEL
# ---------------------------------------------------------------------------


class TestHaltAllowsCancel:
    def test_halt_allows_cancel(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-1_000_000)
        assert fsm.state == StormGuardState.HALT

        intent = make_order_intent(intent_type=IntentType.CANCEL)
        ok, reason = fsm.validate(intent)
        assert ok is True
        assert reason == "OK"


# ---------------------------------------------------------------------------
# 5. Cooldown prevents immediate de-escalation
# ---------------------------------------------------------------------------


class TestCooldownPreventsImmediateDeescalation:
    def test_storm_with_long_cooldown_stays_storm(self) -> None:
        """When cooldown hasn't elapsed, STORM cannot de-escalate."""
        fsm = _fsm(cooldown_s=9999, de_escalate_n=1)
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

        # PnL recovers, but cooldown hasn't elapsed
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.STORM

    @patch("hft_platform.risk.validators.time")
    def test_storm_deescalates_after_cooldown(self, mock_time: MagicMock) -> None:
        """After cooldown elapses, de-escalation proceeds."""
        t = 1000.0
        mock_time.monotonic.return_value = t

        fsm = _fsm(cooldown_s=30.0, de_escalate_n=1)
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

        # Advance past cooldown
        mock_time.monotonic.return_value = t + 31.0
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.NORMAL


# ---------------------------------------------------------------------------
# 6. De-escalation count required
# ---------------------------------------------------------------------------


class TestDeescalationCountRequired:
    def test_needs_n_consecutive_clears(self) -> None:
        """STORM requires N consecutive clear evaluations to de-escalate."""
        fsm = _fsm(cooldown_s=0, de_escalate_n=3)
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

        # 1st clear - not enough
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.STORM

        # 2nd clear - still not enough
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.STORM

        # 3rd clear - de-escalates
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.NORMAL

    def test_counter_resets_on_reescalation(self) -> None:
        """Re-triggering storm resets the de-escalation counter."""
        fsm = _fsm(cooldown_s=0, de_escalate_n=3)
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

        # 2 clears, then re-trigger
        fsm.update_pnl(0)
        fsm.update_pnl(0)
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

        # Need full 3 again
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.STORM
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.STORM
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.NORMAL

    def test_halt_allows_immediate_stepdown(self) -> None:
        """HALT bypasses cooldown and N-count requirements."""
        fsm = _fsm(cooldown_s=9999, de_escalate_n=9999)
        fsm.update_pnl(-1_000_000)
        assert fsm.state == StormGuardState.HALT

        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.NORMAL


# ---------------------------------------------------------------------------
# 7. Parametrized drawdown transitions
# ---------------------------------------------------------------------------


class TestParametrizedDrawdownTransitions:
    @pytest.mark.parametrize(
        "pnl, expected_state",
        [
            (0, StormGuardState.NORMAL),
            (100_000, StormGuardState.NORMAL),
            (-100_000, StormGuardState.NORMAL),
            (-199_999, StormGuardState.NORMAL),
            (-200_000, StormGuardState.WARM),
            (-300_000, StormGuardState.WARM),
            (-499_999, StormGuardState.WARM),
            (-500_000, StormGuardState.STORM),
            (-750_000, StormGuardState.STORM),
            (-999_999, StormGuardState.STORM),
            (-1_000_000, StormGuardState.HALT),
            (-2_000_000, StormGuardState.HALT),
        ],
        ids=[
            "zero",
            "positive_pnl",
            "small_loss",
            "just_above_warm",
            "warm_boundary",
            "mid_warm",
            "just_above_storm",
            "storm_boundary",
            "mid_storm",
            "just_above_halt",
            "halt_boundary",
            "deep_halt",
        ],
    )
    def test_pnl_to_state(self, pnl: int, expected_state: StormGuardState) -> None:
        fsm = _fsm()
        fsm.update_pnl(pnl)
        assert fsm.state == expected_state

    @pytest.mark.parametrize(
        "initial_pnl, initial_state, update_pnl, expected_state",
        [
            # Escalation from NORMAL
            (-100_000, StormGuardState.NORMAL, -500_000, StormGuardState.STORM),
            (-100_000, StormGuardState.NORMAL, -1_000_000, StormGuardState.HALT),
            # Escalation from WARM
            (-200_000, StormGuardState.WARM, -500_000, StormGuardState.STORM),
            (-200_000, StormGuardState.WARM, -1_000_000, StormGuardState.HALT),
            # Escalation from STORM
            (-500_000, StormGuardState.STORM, -1_000_000, StormGuardState.HALT),
        ],
        ids=[
            "normal_to_storm",
            "normal_to_halt",
            "warm_to_storm",
            "warm_to_halt",
            "storm_to_halt",
        ],
    )
    def test_escalation_matrix(
        self,
        initial_pnl: int,
        initial_state: StormGuardState,
        update_pnl: int,
        expected_state: StormGuardState,
    ) -> None:
        fsm = _fsm()
        fsm.update_pnl(initial_pnl)
        assert fsm.state == initial_state

        fsm.update_pnl(update_pnl)
        assert fsm.state == expected_state


# ---------------------------------------------------------------------------
# 8. NORMAL state approves all intent types
# ---------------------------------------------------------------------------


class TestNormalStateApprovesAll:
    @pytest.mark.parametrize(
        "intent_type",
        [IntentType.NEW, IntentType.CANCEL],
        ids=["new", "cancel"],
    )
    def test_normal_approves(self, intent_type: IntentType) -> None:
        fsm = _fsm()
        assert fsm.state == StormGuardState.NORMAL

        intent = make_order_intent(intent_type=intent_type)
        ok, reason = fsm.validate(intent)
        assert ok is True
        assert reason == "OK"

    @pytest.mark.parametrize(
        "side",
        [Side.BUY, Side.SELL],
        ids=["buy", "sell"],
    )
    def test_normal_approves_all_sides(self, side: Side) -> None:
        fsm = _fsm()
        intent = make_order_intent(intent_type=IntentType.NEW, side=side)
        ok, reason = fsm.validate(intent)
        assert ok is True
        assert reason == "OK"


# ---------------------------------------------------------------------------
# Additional: STORM blocks NEW but allows CANCEL
# ---------------------------------------------------------------------------


class TestStormValidation:
    def test_storm_blocks_new(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

        intent = make_order_intent(intent_type=IntentType.NEW)
        ok, reason = fsm.validate(intent)
        assert ok is False
        assert "STORM" in reason

    def test_storm_allows_cancel(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

        intent = make_order_intent(intent_type=IntentType.CANCEL)
        ok, reason = fsm.validate(intent)
        assert ok is True

    def test_warm_allows_new(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-200_000)
        assert fsm.state == StormGuardState.WARM

        intent = make_order_intent(intent_type=IntentType.NEW)
        ok, reason = fsm.validate(intent)
        assert ok is True
