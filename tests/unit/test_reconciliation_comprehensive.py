"""Comprehensive tests for execution/reconciliation module.

Covers PositionDiscrepancy, _compute_backoff_delay, and ReconciliationService.
"""

from __future__ import annotations

import random
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.execution.reconciliation import (
    PositionDiscrepancy,
    ReconciliationService,
    _compute_backoff_delay,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_recon_service(positions=None, broker_positions=None, config=None):
    """Build a ReconciliationService with mocked dependencies."""
    client = MagicMock()
    client.get_positions = MagicMock(return_value=broker_positions or [])

    store = MagicMock()
    store.positions = positions or {}

    storm_guard = MagicMock()

    with patch("hft_platform.execution.reconciliation.MetricsRegistry") as m:
        registry = MagicMock()
        m.get.return_value = registry
        svc = ReconciliationService(client, store, config or {}, storm_guard)
    return svc, storm_guard


# ===================================================================
# PositionDiscrepancy tests (6)
# ===================================================================


class TestPositionDiscrepancy:
    """Tests for PositionDiscrepancy dataclass properties."""

    def test_both_zero_not_critical(self):
        """Both local and broker at zero -> not critical."""
        d = PositionDiscrepancy(symbol="2330", local_qty=0, broker_qty=0, diff=0)
        assert d.is_critical is False

    def test_sign_mismatch_is_critical(self):
        """Local long, broker short (or vice versa) -> critical."""
        d = PositionDiscrepancy(symbol="2330", local_qty=50, broker_qty=-10, diff=60)
        assert d.is_critical is True

        d2 = PositionDiscrepancy(symbol="2317", local_qty=-30, broker_qty=5, diff=-35)
        assert d2.is_critical is True

    def test_large_absolute_diff_is_critical(self):
        """abs(diff) > max(100, abs(local)//10) -> critical."""
        # local=500, threshold = max(100, 50) = 100; diff=101 > 100
        d = PositionDiscrepancy(symbol="2330", local_qty=500, broker_qty=399, diff=101)
        assert d.is_critical is True

        # local=2000, threshold = max(100, 200) = 200; diff=201 > 200
        d2 = PositionDiscrepancy(symbol="2317", local_qty=2000, broker_qty=1799, diff=201)
        assert d2.is_critical is True

    def test_small_diff_not_critical(self):
        """Small diff within threshold -> not critical."""
        # local=1000, threshold = max(100, 100) = 100; diff=5 <= 100
        d = PositionDiscrepancy(symbol="2330", local_qty=1000, broker_qty=995, diff=5)
        assert d.is_critical is False

    def test_severity_critical(self):
        """is_critical -> severity == 'critical'."""
        d = PositionDiscrepancy(symbol="2330", local_qty=50, broker_qty=-10, diff=60)
        assert d.severity == "critical"

    def test_severity_warning_vs_info(self):
        """abs(diff) > 10 -> 'warning'; <= 10 -> 'info'."""
        warning = PositionDiscrepancy(symbol="2330", local_qty=1000, broker_qty=989, diff=11)
        assert warning.severity == "warning"

        info = PositionDiscrepancy(symbol="2317", local_qty=1000, broker_qty=992, diff=8)
        assert info.severity == "info"


# ===================================================================
# _compute_backoff_delay tests (3)
# ===================================================================


class TestComputeBackoffDelay:
    """Tests for exponential backoff helper."""

    def test_first_attempt_equals_base(self):
        """attempt=0, jitter=0 -> raw = base^1 = base exactly."""
        result = _compute_backoff_delay(attempt=0, base=2.0, max_delay=60.0, jitter=0.0)
        assert result == pytest.approx(2.0)

    def test_respects_max_delay_cap(self):
        """Large attempt should be capped at max_delay."""
        result = _compute_backoff_delay(attempt=100, base=2.0, max_delay=60.0, jitter=0.0)
        assert result == pytest.approx(60.0)

    def test_jitter_produces_variation(self):
        """With jitter > 0, repeated calls yield different values within bounds."""
        random.seed(1)
        vals = [_compute_backoff_delay(attempt=0, base=2.0, max_delay=60.0, jitter=0.2) for _ in range(20)]
        # All values should be in [base*(1-jitter), base*(1+jitter)] = [1.6, 2.4]
        for v in vals:
            assert 1.6 <= v <= 2.4, f"value {v} outside jitter range"
        # At least two distinct values
        assert len(set(vals)) > 1


# ===================================================================
# ReconciliationService tests (7)
# ===================================================================


class TestReconciliationService:
    """Tests for ReconciliationService core logic."""

    def test_compute_discrepancies_matching_positions(self):
        """When local and broker match exactly -> no discrepancies."""
        svc, _ = make_recon_service()
        result = svc._compute_discrepancies(
            {"2330": 100, "2317": -50},
            {"2330": 100, "2317": -50},
        )
        assert result == []

    def test_compute_discrepancies_broker_only_symbol(self):
        """Symbol exists at broker but not locally -> discrepancy."""
        svc, _ = make_recon_service()
        result = svc._compute_discrepancies({}, {"2330": 100})
        assert len(result) == 1
        d = result[0]
        assert d.symbol == "2330"
        assert d.local_qty == 0
        assert d.broker_qty == 100
        assert d.diff == -100

    def test_compute_discrepancies_local_only_symbol(self):
        """Symbol exists locally but not at broker -> discrepancy."""
        svc, _ = make_recon_service()
        result = svc._compute_discrepancies({"2330": 50}, {})
        assert len(result) == 1
        d = result[0]
        assert d.symbol == "2330"
        assert d.local_qty == 50
        assert d.broker_qty == 0
        assert d.diff == 50

    @pytest.mark.asyncio
    async def test_sync_portfolio_success_path(self):
        """sync_portfolio fetches broker positions and computes discrepancies."""
        local_pos = MagicMock()
        local_pos.symbol = "2330"
        local_pos.net_qty = 100
        local_pos.strategy_id = "default"

        broker_pos = MagicMock()
        broker_pos.code = "2330"
        broker_pos.quantity = 100
        broker_pos.direction = "Action.Buy"

        svc, _ = make_recon_service(
            positions={("account", "2330"): local_pos},
            broker_positions=[broker_pos],
        )

        with (
            patch.object(svc, "_metrics") as mock_metrics,
            patch(
                "hft_platform.execution.reconciliation.asyncio.to_thread",
                new=AsyncMock(return_value=[broker_pos]),
            ),
        ):
            mock_registry = MagicMock()
            mock_metrics.return_value = mock_registry
            await svc.sync_portfolio()

        assert isinstance(svc._last_discrepancies, list)

    @pytest.mark.asyncio
    async def test_run_loop_consecutive_failures_increment(self):
        """Consecutive sync failures increment the failure counter."""
        svc, storm_guard = make_recon_service(
            config={"reconciliation": {"check_interval_s": 0.01, "grace_failures": 100}},
        )

        call_count = 0

        async def sync_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return  # startup sync succeeds
            if call_count >= 5:
                svc.running = False
            raise RuntimeError("broker unavailable")

        with (
            patch.object(svc, "sync_portfolio", side_effect=sync_side_effect),
            patch.object(svc, "_update_failure_gauge"),
            # Bug #38 added an off-session gate; force in-session so the failure
            # counter actually increments regardless of when CI runs.
            patch.object(svc, "_in_trading_hours", return_value=True),
            patch(
                "hft_platform.execution.reconciliation._compute_backoff_delay",
                return_value=0.001,
            ),
        ):
            await svc.run()

        assert svc._consecutive_failures >= 3

    @pytest.mark.asyncio
    async def test_run_loop_grace_failures_triggers_halt(self):
        """When consecutive failures reach grace_failures, HALT is triggered."""
        svc, storm_guard = make_recon_service(
            config={"reconciliation": {"check_interval_s": 0.01, "grace_failures": 2}},
        )

        call_count = 0

        async def sync_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return  # startup sync succeeds
            if call_count >= 5:
                svc.running = False
                return
            raise RuntimeError("broker down")

        with (
            patch.object(svc, "sync_portfolio", side_effect=sync_side_effect),
            patch.object(svc, "_update_failure_gauge"),
            # Bug #38 off-session gate: force in-session so HALT path is reachable.
            patch.object(svc, "_in_trading_hours", return_value=True),
            patch(
                "hft_platform.execution.reconciliation._compute_backoff_delay",
                return_value=0.001,
            ),
        ):
            await svc.run()

        storm_guard.trigger_halt.assert_called_once()
        assert "RECONCILIATION_UNAVAILABLE" in storm_guard.trigger_halt.call_args[0][0]

    @pytest.mark.asyncio
    async def test_run_loop_off_session_does_not_escalate_to_halt(self):
        """Bug #38: outside TAIFEX trading hours, repeated sync failures must
        NOT increment the failure counter and must NOT trigger HALT — broker
        often returns None/errors when the market is closed (overnight gap),
        and the previous behaviour produced false-positive HALT cycles."""
        svc, storm_guard = make_recon_service(
            config={"reconciliation": {"check_interval_s": 0.01, "grace_failures": 2}},
        )

        call_count = 0

        async def sync_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return  # startup sync succeeds
            if call_count >= 8:
                svc.running = False
                return
            raise RuntimeError("get_positions returned None")

        with (
            patch.object(svc, "sync_portfolio", side_effect=sync_side_effect),
            patch.object(svc, "_update_failure_gauge"),
            patch.object(svc, "_in_trading_hours", return_value=False),
            patch(
                "hft_platform.execution.reconciliation._compute_backoff_delay",
                return_value=0.001,
            ),
        ):
            await svc.run()

        storm_guard.trigger_halt.assert_not_called()
        assert svc._consecutive_failures == 0
        assert svc._halt_triggered is False

    @pytest.mark.asyncio
    async def test_run_loop_in_session_still_escalates(self):
        """Sanity check: with the off-session gate explicitly inactive, the
        legacy HALT-on-grace-exceeded path still fires."""
        svc, storm_guard = make_recon_service(
            config={"reconciliation": {"check_interval_s": 0.01, "grace_failures": 2}},
        )

        call_count = 0

        async def sync_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return
            if call_count >= 5:
                svc.running = False
                return
            raise RuntimeError("broker down")

        with (
            patch.object(svc, "sync_portfolio", side_effect=sync_side_effect),
            patch.object(svc, "_update_failure_gauge"),
            patch.object(svc, "_in_trading_hours", return_value=True),
            patch(
                "hft_platform.execution.reconciliation._compute_backoff_delay",
                return_value=0.001,
            ),
        ):
            await svc.run()

        storm_guard.trigger_halt.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_loop_success_resets_consecutive_failures(self):
        """A successful sync resets _consecutive_failures to 0."""
        svc, _ = make_recon_service(
            config={"reconciliation": {"check_interval_s": 0.01, "grace_failures": 100}},
        )

        call_count = 0

        async def sync_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return  # startup sync succeeds
            if call_count == 2:
                raise RuntimeError("transient error")
            if call_count == 3:
                raise RuntimeError("transient error")
            if call_count == 4:
                pass  # success — resets counter
            if call_count >= 5:
                svc.running = False

        with (
            patch.object(svc, "sync_portfolio", side_effect=sync_side_effect),
            patch.object(svc, "_update_failure_gauge"),
            patch(
                "hft_platform.execution.reconciliation._compute_backoff_delay",
                return_value=0.001,
            ),
        ):
            await svc.run()

        assert svc._consecutive_failures == 0
