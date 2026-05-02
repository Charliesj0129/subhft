"""Parametrized test matrix: StormGuard state × IntentType.

Covers all 12 combinations (4 states × 3 intent types) to verify that
the StormGuardFSM.validate() method enforces the correct allow/reject
policy per state.

Expected behavior (from source):
  NORMAL / WARM  → all intents allowed
  STORM          → NEW blocked, AMEND + CANCEL allowed
  HALT           → only CANCEL allowed
"""

from __future__ import annotations

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

_DEFAULT_CONFIG: dict = {
    "storm_guard": {
        "warm_threshold": -200_000,
        "storm_threshold": -500_000,
        "halt_threshold": -1_000_000,
    },
}


def _make_intent(intent_type: IntentType) -> OrderIntent:
    """Create a minimal OrderIntent for testing."""
    return OrderIntent(
        intent_id=1,
        strategy_id="test_strategy",
        symbol="2330",
        intent_type=intent_type,
        side=Side.BUY,
        price=100_0000,  # scaled x10000
        qty=1,
    )


def _make_fsm(state: StormGuardState) -> StormGuardFSM:
    """Create a StormGuardFSM and force it into *state*."""
    with patch("hft_platform.risk.storm_guard.MetricsRegistry") as mock_registry_cls:
        mock_metrics = MagicMock()
        mock_registry_cls.get.return_value = mock_metrics
        fsm = StormGuardFSM(_DEFAULT_CONFIG)

    fsm.state = state
    return fsm


# ---------------------------------------------------------------------------
# Parametrized matrix
# ---------------------------------------------------------------------------

# (state, intent_type, expected_ok)
_MATRIX: list[tuple[StormGuardState, IntentType, bool]] = [
    # NORMAL — all allowed
    (StormGuardState.NORMAL, IntentType.NEW, True),
    (StormGuardState.NORMAL, IntentType.AMEND, True),
    (StormGuardState.NORMAL, IntentType.CANCEL, True),
    # WARM — all allowed
    (StormGuardState.WARM, IntentType.NEW, True),
    (StormGuardState.WARM, IntentType.AMEND, True),
    (StormGuardState.WARM, IntentType.CANCEL, True),
    # STORM — NEW + AMEND blocked, CANCEL allowed
    (StormGuardState.STORM, IntentType.NEW, False),
    (StormGuardState.STORM, IntentType.AMEND, False),
    (StormGuardState.STORM, IntentType.CANCEL, True),
    # HALT — only CANCEL allowed
    (StormGuardState.HALT, IntentType.NEW, False),
    (StormGuardState.HALT, IntentType.AMEND, False),
    (StormGuardState.HALT, IntentType.CANCEL, True),
]


@pytest.mark.parametrize(
    ("state", "intent_type", "expected_ok"),
    _MATRIX,
    ids=[f"{s.name}-{it.name}" for s, it, _ in _MATRIX],
)
def test_stormguard_validate_matrix(
    state: StormGuardState,
    intent_type: IntentType,
    expected_ok: bool,
) -> None:
    fsm = _make_fsm(state)
    intent = _make_intent(intent_type)

    ok, reason = fsm.validate(intent)

    assert ok is expected_ok, (
        f"state={state.name}, intent={intent_type.name}: expected ok={expected_ok}, got ok={ok} reason={reason!r}"
    )

    if expected_ok:
        assert reason == "OK"
    else:
        assert reason != "OK"
        assert "STORMGUARD" in reason


# ---------------------------------------------------------------------------
# Reason code verification
# ---------------------------------------------------------------------------


class TestStormGuardReasonCodes:
    """Verify specific reason strings for rejected intents."""

    def test_halt_new_reason(self) -> None:
        fsm = _make_fsm(StormGuardState.HALT)
        ok, reason = fsm.validate(_make_intent(IntentType.NEW))
        assert not ok
        assert reason == "STORMGUARD_HALT"

    def test_halt_amend_reason(self) -> None:
        fsm = _make_fsm(StormGuardState.HALT)
        ok, reason = fsm.validate(_make_intent(IntentType.AMEND))
        assert not ok
        assert reason == "STORMGUARD_HALT"

    def test_storm_new_reason(self) -> None:
        fsm = _make_fsm(StormGuardState.STORM)
        ok, reason = fsm.validate(_make_intent(IntentType.NEW))
        assert not ok
        assert reason == "STORMGUARD_STORM_BLOCKED"

    def test_halt_cancel_allowed(self) -> None:
        fsm = _make_fsm(StormGuardState.HALT)
        ok, reason = fsm.validate(_make_intent(IntentType.CANCEL))
        assert ok
        assert reason == "OK"
