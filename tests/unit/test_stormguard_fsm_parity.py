"""Parametrized boundary and parity tests for StormGuardFSM in validators.py.

Mirrors the patterns from test_stormguard_state_machine.py but targets the
StormGuardFSM class (config-dict-driven, PnL-based thresholds, validate() API).
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    IntentType,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.risk.validators import StormGuardFSM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: Dict[str, Any] = {
    "storm_guard": {
        "warm_threshold": -200_000,
        "storm_threshold": -500_000,
        "halt_threshold": -1_000_000,
    },
}


def _fsm(
    config: Dict[str, Any] | None = None,
    cooldown_s: float = 0.0,
    de_escalate_n: int = 1,
) -> StormGuardFSM:
    """Create a StormGuardFSM with hysteresis overrides for deterministic tests."""
    fsm = StormGuardFSM(config or _DEFAULT_CONFIG)
    fsm._storm_cooldown_s = cooldown_s
    fsm._de_escalate_threshold = de_escalate_n
    return fsm


def _intent(intent_type: IntentType = IntentType.NEW) -> OrderIntent:
    """Create a minimal OrderIntent for validation tests."""
    return OrderIntent(
        intent_id=1,
        strategy_id="test_strat",
        symbol="2330",
        intent_type=intent_type,
        side=Side.BUY,
        price=100_0000,  # 100.0 scaled x10000
        qty=1,
    )


# ---------------------------------------------------------------------------
# Parametrized drawdown threshold transitions
# ---------------------------------------------------------------------------


class TestDrawdownEscalation:
    """Boundary tests for PnL-driven state transitions."""

    @pytest.mark.parametrize(
        "pnl, expected",
        [
            (0, StormGuardState.NORMAL),
            (-100_000, StormGuardState.NORMAL),
            (-199_999, StormGuardState.NORMAL),
            (-200_000, StormGuardState.WARM),
            (-200_001, StormGuardState.WARM),
            (-350_000, StormGuardState.WARM),
            (-499_999, StormGuardState.WARM),
            (-500_000, StormGuardState.STORM),
            (-500_001, StormGuardState.STORM),
            (-750_000, StormGuardState.STORM),
            (-999_999, StormGuardState.STORM),
            (-1_000_000, StormGuardState.HALT),
            (-1_000_001, StormGuardState.HALT),
            (-5_000_000, StormGuardState.HALT),
        ],
        ids=[
            "zero",
            "mid_normal",
            "just_above_warm",
            "warm_boundary",
            "just_below_warm",
            "mid_warm",
            "just_above_storm",
            "storm_boundary",
            "just_below_storm",
            "mid_storm",
            "just_above_halt",
            "halt_boundary",
            "just_below_halt",
            "deep_halt",
        ],
    )
    def test_drawdown_escalation(self, pnl: int, expected: StormGuardState) -> None:
        fsm = _fsm()
        fsm.update_pnl(pnl)
        assert fsm.state == expected

    def test_positive_pnl_stays_normal(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(500_000)
        assert fsm.state == StormGuardState.NORMAL

    def test_escalation_chain(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-200_000)
        assert fsm.state == StormGuardState.WARM
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM
        fsm.update_pnl(-1_000_000)
        assert fsm.state == StormGuardState.HALT

    def test_escalation_is_instant_ignores_hysteresis(self) -> None:
        """Escalation must not be gated by cooldown or consecutive checks."""
        fsm = _fsm(cooldown_s=9999, de_escalate_n=9999)
        fsm.update_pnl(-1_000_000)
        assert fsm.state == StormGuardState.HALT

    def test_skip_normal_to_halt(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-1_000_000)
        assert fsm.state == StormGuardState.HALT

    def test_skip_normal_to_storm(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM


# ---------------------------------------------------------------------------
# De-escalation with hysteresis
# ---------------------------------------------------------------------------


class TestDeEscalation:
    def test_halt_allows_immediate_step_down(self) -> None:
        """HALT has special handling: immediate de-escalation (no cooldown/N)."""
        fsm = _fsm(cooldown_s=9999, de_escalate_n=9999)
        fsm.update_pnl(-1_000_000)
        assert fsm.state == StormGuardState.HALT
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.NORMAL

    def test_halt_to_warm(self) -> None:
        fsm = _fsm(cooldown_s=9999, de_escalate_n=9999)
        fsm.update_pnl(-1_000_000)
        assert fsm.state == StormGuardState.HALT
        fsm.update_pnl(-200_000)
        assert fsm.state == StormGuardState.WARM

    def test_halt_to_storm(self) -> None:
        fsm = _fsm(cooldown_s=9999, de_escalate_n=9999)
        fsm.update_pnl(-1_000_000)
        assert fsm.state == StormGuardState.HALT
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

    def test_storm_requires_n_consecutive_clears(self) -> None:
        fsm = _fsm(cooldown_s=0, de_escalate_n=3)
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

        # 1st clear
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.STORM
        # 2nd clear
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.STORM
        # 3rd clear => de-escalate
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.NORMAL

    def test_storm_counter_reset_on_re_escalation(self) -> None:
        fsm = _fsm(cooldown_s=0, de_escalate_n=3)
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

        # 2 clears, then re-trigger (same-state re-entry resets counter)
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

    def test_warm_de_escalation_no_storm_cooldown(self) -> None:
        """WARM->NORMAL does not require storm cooldown (no storm entry)."""
        fsm = _fsm(cooldown_s=0, de_escalate_n=1)
        fsm.update_pnl(-200_000)
        assert fsm.state == StormGuardState.WARM
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.NORMAL

    def test_storm_cooldown_blocks_de_escalation(self) -> None:
        """When cooldown has not elapsed, de-escalation counter resets."""
        fsm = _fsm(cooldown_s=9999, de_escalate_n=1)
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.STORM

    @patch("hft_platform.risk.validators.time.monotonic")
    def test_storm_cooldown_elapsed_allows_de_escalation(
        self, mock_monotonic: MagicMock
    ) -> None:
        t = 1000.0
        mock_monotonic.return_value = t
        fsm = _fsm(cooldown_s=30.0, de_escalate_n=1)

        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

        # Advance past cooldown
        mock_monotonic.return_value = t + 31.0
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.NORMAL


# ---------------------------------------------------------------------------
# validate() per state: NEW / AMEND / CANCEL
# ---------------------------------------------------------------------------


class TestValidatePerState:
    """Check that validate() blocks/allows intents correctly per FSM state."""

    @pytest.mark.parametrize(
        "state, intent_type, expected_safe",
        [
            # NORMAL: everything allowed
            (StormGuardState.NORMAL, IntentType.NEW, True),
            (StormGuardState.NORMAL, IntentType.AMEND, True),
            (StormGuardState.NORMAL, IntentType.CANCEL, True),
            # WARM: everything allowed
            (StormGuardState.WARM, IntentType.NEW, True),
            (StormGuardState.WARM, IntentType.AMEND, True),
            (StormGuardState.WARM, IntentType.CANCEL, True),
            # STORM: NEW blocked, AMEND/CANCEL allowed
            (StormGuardState.STORM, IntentType.NEW, False),
            (StormGuardState.STORM, IntentType.AMEND, True),
            (StormGuardState.STORM, IntentType.CANCEL, True),
            # HALT: only CANCEL allowed
            (StormGuardState.HALT, IntentType.NEW, False),
            (StormGuardState.HALT, IntentType.AMEND, False),
            (StormGuardState.HALT, IntentType.CANCEL, True),
        ],
        ids=[
            "normal_new",
            "normal_amend",
            "normal_cancel",
            "warm_new",
            "warm_amend",
            "warm_cancel",
            "storm_new",
            "storm_amend",
            "storm_cancel",
            "halt_new",
            "halt_amend",
            "halt_cancel",
        ],
    )
    def test_validate_intent(
        self,
        state: StormGuardState,
        intent_type: IntentType,
        expected_safe: bool,
    ) -> None:
        fsm = _fsm()
        fsm.state = state
        approved, reason = fsm.validate(_intent(intent_type))
        assert approved is expected_safe, f"state={state.name}, type={intent_type.name}, reason={reason}"

    def test_cancel_always_allowed_across_all_states(self) -> None:
        """CANCEL orders must pass validate() in every state."""
        cancel = _intent(IntentType.CANCEL)
        for state in StormGuardState:
            fsm = _fsm()
            fsm.state = state
            approved, _ = fsm.validate(cancel)
            assert approved is True, f"CANCEL rejected in state {state.name}"


# ---------------------------------------------------------------------------
# validate() reason codes
# ---------------------------------------------------------------------------


class TestValidateReasonCodes:
    def test_halt_reason_code(self) -> None:
        fsm = _fsm()
        fsm.state = StormGuardState.HALT
        _, reason = fsm.validate(_intent(IntentType.NEW))
        assert reason == "STORMGUARD_HALT"

    def test_storm_new_reason_code(self) -> None:
        fsm = _fsm()
        fsm.state = StormGuardState.STORM
        _, reason = fsm.validate(_intent(IntentType.NEW))
        assert reason == "STORMGUARD_STORM_NEW_BLOCKED"

    def test_ok_reason_code(self) -> None:
        fsm = _fsm()
        fsm.state = StormGuardState.NORMAL
        _, reason = fsm.validate(_intent(IntentType.NEW))
        assert reason == "OK"


# ---------------------------------------------------------------------------
# Multi-step scenarios
# ---------------------------------------------------------------------------


class TestMultiStep:
    def test_full_cycle_escalate_and_recover(self) -> None:
        fsm = _fsm(cooldown_s=0, de_escalate_n=1)
        assert fsm.state == StormGuardState.NORMAL

        fsm.update_pnl(-200_000)
        assert fsm.state == StormGuardState.WARM

        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

        fsm.update_pnl(-1_000_000)
        assert fsm.state == StormGuardState.HALT

        # HALT immediate step-down
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

        # De-escalate from STORM (cooldown=0, n=1)
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.NORMAL

    def test_repeated_normal_stays_normal(self) -> None:
        fsm = _fsm()
        for _ in range(20):
            fsm.update_pnl(0)
        assert fsm.state == StormGuardState.NORMAL

    def test_oscillating_pnl_respects_hysteresis(self) -> None:
        """Rapidly alternating PnL should respect N-consecutive-clear hysteresis."""
        fsm = _fsm(cooldown_s=0, de_escalate_n=3)
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

        # Oscillate: 2 clears then re-trigger
        for _ in range(5):
            fsm.update_pnl(0)
            fsm.update_pnl(0)
            fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

    def test_validate_tracks_state_after_pnl_changes(self) -> None:
        """Validate results must reflect the latest state after PnL updates."""
        fsm = _fsm(cooldown_s=0, de_escalate_n=1)
        new_intent = _intent(IntentType.NEW)

        # NORMAL: NEW allowed
        approved, _ = fsm.validate(new_intent)
        assert approved is True

        # Escalate to STORM: NEW blocked
        fsm.update_pnl(-500_000)
        approved, _ = fsm.validate(new_intent)
        assert approved is False

        # Recover to NORMAL: NEW allowed again
        fsm.update_pnl(0)
        approved, _ = fsm.validate(new_intent)
        assert approved is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_initial_state_is_normal(self) -> None:
        fsm = _fsm()
        assert fsm.state == StormGuardState.NORMAL

    def test_custom_thresholds(self) -> None:
        config: Dict[str, Any] = {
            "storm_guard": {
                "warm_threshold": -100,
                "storm_threshold": -200,
                "halt_threshold": -300,
            },
        }
        fsm = _fsm(config=config)
        fsm.update_pnl(-100)
        assert fsm.state == StormGuardState.WARM
        fsm.update_pnl(-200)
        assert fsm.state == StormGuardState.STORM
        fsm.update_pnl(-300)
        assert fsm.state == StormGuardState.HALT

    def test_extreme_negative_pnl(self) -> None:
        fsm = _fsm()
        fsm.update_pnl(-999_999_999)
        assert fsm.state == StormGuardState.HALT

    def test_env_override_cooldown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_STORMGUARD_STORM_COOLDOWN_S", "120")
        fsm = StormGuardFSM(_DEFAULT_CONFIG)
        assert fsm._storm_cooldown_s == 120.0

    def test_env_override_de_escalate_n(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_STORMGUARD_DE_ESCALATE_N", "10")
        fsm = StormGuardFSM(_DEFAULT_CONFIG)
        assert fsm._de_escalate_threshold == 10

    def test_missing_storm_guard_config_uses_defaults(self) -> None:
        fsm = StormGuardFSM({})
        assert fsm.warm == -200_000
        assert fsm.storm == -500_000
        assert fsm.halt == -1_000_000
