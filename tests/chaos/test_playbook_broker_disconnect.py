"""Chaos Playbook 1 — Broker Disconnect.

Simulates broker connectivity loss and verifies the platform enters
reduce-only mode, blocks new risk-opening orders, allows closes,
and restores normal trading on reconnect.
"""

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType
from hft_platform.ops.platform_degrade import PlatformDegradeController


@pytest.fixture()
def controller() -> PlatformDegradeController:
    """Create a fresh PlatformDegradeController with mocked dependencies."""
    with patch("hft_platform.ops.platform_degrade.get_shared_autonomy_evidence_writer", return_value=MagicMock()):
        ctrl = PlatformDegradeController(metrics=MagicMock(), evidence_writer=MagicMock())
    return ctrl


@pytest.mark.chaos
class TestPlaybookBrokerDisconnect:
    """Chaos tests for broker disconnect scenario."""

    def test_disconnect_triggers_reduce_only(self, controller: PlatformDegradeController) -> None:
        """Broker disconnect triggers reduce-only mode via PlatformDegradeController."""
        assert not controller.reduce_only_active

        transition = controller.enter_reduce_only(reason="broker_unavailable")

        assert controller.reduce_only_active
        assert transition.to_mode.value == "PLATFORM_REDUCE_ONLY"
        assert transition.reason == "broker_unavailable"

    def test_reduce_only_blocks_new_opens(self, controller: PlatformDegradeController) -> None:
        """In reduce-only mode, NEW intents that open risk are blocked."""
        controller.enter_reduce_only(reason="broker_unavailable")

        allowed = controller.allow_intent(intent_type=IntentType.NEW, opens_risk=True)

        assert not allowed

    def test_reduce_only_allows_close_orders(self, controller: PlatformDegradeController) -> None:
        """In reduce-only mode, NEW intents that close risk are still allowed."""
        controller.enter_reduce_only(reason="broker_unavailable")

        allowed_close = controller.allow_intent(intent_type=IntentType.NEW, opens_risk=False)
        allowed_cancel = controller.allow_intent(intent_type=IntentType.CANCEL, opens_risk=False)
        allowed_force_flat = controller.allow_intent(intent_type=IntentType.FORCE_FLAT, opens_risk=False)

        assert allowed_close
        assert allowed_cancel
        assert allowed_force_flat

    def test_reconnect_restores_normal_mode(self, controller: PlatformDegradeController) -> None:
        """Exiting reduce-only restores normal trading after reconnect."""
        controller.enter_reduce_only(reason="broker_unavailable")
        assert controller.reduce_only_active

        transition = controller.exit_reduce_only(reason="broker_reconnected")

        assert not controller.reduce_only_active
        assert transition.to_mode.value == "NORMAL"
        # Verify NEW opens are allowed again
        assert controller.allow_intent(intent_type=IntentType.NEW, opens_risk=True)
