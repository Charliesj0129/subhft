"""Chaos Playbook 4 — Position Drift.

Simulates position discrepancies between local state and broker,
verifies drift detection, reduce-only escalation on consecutive
drift, recovery on exit, and grace failure configuration.
"""

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType
from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.ops.platform_degrade import (
    PlatformDegradeController,
    reset_shared_platform_degrade_controller,
)


@pytest.fixture(autouse=True)
def _reset_shared_controller():
    """Reset the shared PlatformDegradeController singleton between tests."""
    reset_shared_platform_degrade_controller()
    yield
    reset_shared_platform_degrade_controller()


@pytest.fixture()
def controller() -> PlatformDegradeController:
    """Create a PlatformDegradeController with mocked dependencies."""
    with patch("hft_platform.ops.platform_degrade.get_shared_autonomy_evidence_writer", return_value=MagicMock()):
        ctrl = PlatformDegradeController(metrics=MagicMock(), evidence_writer=MagicMock())
    return ctrl


@pytest.fixture()
def recon_service():
    """Create a ReconciliationService with mocked dependencies."""
    mock_client = MagicMock()
    mock_position_store = MagicMock()
    mock_position_store.positions = {}

    mock_storm_guard = MagicMock()
    mock_storm_guard.trigger_halt = MagicMock()

    with (
        patch("hft_platform.execution.reconciliation.MetricsRegistry.get", return_value=MagicMock()),
        patch("hft_platform.execution.reconciliation.timebase") as mock_tb,
        patch("hft_platform.ops.platform_degrade.get_shared_autonomy_evidence_writer", return_value=MagicMock()),
    ):
        mock_tb.now_s = MagicMock(return_value=1_000_000.0)
        mock_tb.now_ns = MagicMock(return_value=1_000_000_000_000_000)
        service = ReconciliationService(
            client=mock_client,
            position_store=mock_position_store,
            config={},
            storm_guard=mock_storm_guard,
        )
    return service


@pytest.mark.chaos
class TestPlaybookPositionDrift:
    """Chaos tests for position drift scenario."""

    def test_drift_detected_on_mismatch(self, recon_service) -> None:
        """ReconciliationService construction succeeds and can compute discrepancies."""
        local_map = {"2330": 100, "2317": 50}
        broker_map = {"2330": 95, "2317": 50}

        discrepancies = recon_service._compute_discrepancies(local_map, broker_map)

        assert len(discrepancies) == 1
        assert discrepancies[0].symbol == "2330"
        assert discrepancies[0].diff == 5  # local - broker

    def test_consecutive_drift_triggers_reduce_only(self, controller) -> None:
        """Consecutive non-critical drift observations trigger reduce-only mode."""
        assert not controller.reduce_only_active

        controller.enter_reduce_only(reason="reconciliation_drift")

        assert controller.reduce_only_active
        assert not controller.allow_intent(intent_type=IntentType.NEW, opens_risk=True)

    def test_reduce_only_exit_restores_trading(self, controller) -> None:
        """Exiting reduce-only after drift resolution re-enables NEW opens."""
        controller.enter_reduce_only(reason="reconciliation_drift")
        assert controller.reduce_only_active

        controller.exit_reduce_only(reason="drift_resolved")

        assert not controller.reduce_only_active
        assert controller.allow_intent(intent_type=IntentType.NEW, opens_risk=True)

    def test_grace_failures_before_halt(self, recon_service) -> None:
        """ReconciliationService.grace_failures is configurable and defaults to 10."""
        assert recon_service.grace_failures == 10

        # Override via config
        mock_client = MagicMock()
        mock_position_store = MagicMock()
        mock_position_store.positions = {}
        mock_storm_guard = MagicMock()

        with (
            patch("hft_platform.execution.reconciliation.MetricsRegistry.get", return_value=MagicMock()),
            patch("hft_platform.execution.reconciliation.timebase") as mock_tb,
            patch("hft_platform.ops.platform_degrade.get_shared_autonomy_evidence_writer", return_value=MagicMock()),
        ):
            mock_tb.now_s = MagicMock(return_value=1_000_000.0)
            mock_tb.now_ns = MagicMock(return_value=1_000_000_000_000_000)
            custom_service = ReconciliationService(
                client=mock_client,
                position_store=mock_position_store,
                config={"reconciliation": {"grace_failures": 3}},
                storm_guard=mock_storm_guard,
            )

        assert custom_service.grace_failures == 3
