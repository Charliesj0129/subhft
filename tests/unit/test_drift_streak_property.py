"""Tests for ReconciliationService.drift_streak property."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.execution.reconciliation import ReconciliationService


def _make_recon_service() -> ReconciliationService:
    """Create a ReconciliationService with mocked dependencies."""
    client = MagicMock()
    client.get_positions = MagicMock(return_value=[])
    position_store = MagicMock()
    position_store.positions = {}
    config: dict = {}
    storm_guard = MagicMock()
    return ReconciliationService(
        client=client,
        position_store=position_store,
        config=config,
        storm_guard=storm_guard,
    )


class TestDriftStreakProperty:
    def test_initial_drift_streak_is_zero(self) -> None:
        svc = _make_recon_service()
        assert svc.drift_streak == 0

    def test_drift_streak_reflects_internal_counter(self) -> None:
        svc = _make_recon_service()
        svc._noncritical_drift_streak = 5
        assert svc.drift_streak == 5

    def test_drift_streak_is_read_only(self) -> None:
        svc = _make_recon_service()
        with pytest.raises(AttributeError):
            svc.drift_streak = 10  # type: ignore[misc]

    def test_drift_streak_type_is_int(self) -> None:
        svc = _make_recon_service()
        assert isinstance(svc.drift_streak, int)
