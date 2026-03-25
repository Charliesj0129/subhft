"""Tests for ReconciliationService.drift_streak property."""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.execution.reconciliation import ReconciliationService


def _make_service() -> ReconciliationService:
    client = MagicMock()
    position_store = MagicMock()
    position_store.positions = {}
    config: dict = {"reconciliation": {"check_interval_s": 1}}
    storm_guard = MagicMock()
    return ReconciliationService(client, position_store, config, storm_guard)


class TestDriftStreakProperty:
    def test_drift_streak_initial_value_is_zero(self) -> None:
        svc = _make_service()
        assert svc.drift_streak == 0

    def test_drift_streak_reflects_internal_counter(self) -> None:
        svc = _make_service()
        svc._noncritical_drift_streak = 5
        assert svc.drift_streak == 5

    def test_drift_streak_is_readonly(self) -> None:
        svc = _make_service()
        # Property should not have a setter
        assert not hasattr(ReconciliationService.drift_streak, "fset") or ReconciliationService.drift_streak.fset is None
