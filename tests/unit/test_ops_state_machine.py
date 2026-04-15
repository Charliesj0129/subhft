"""Tests for OperationsStateMachine daily lifecycle."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


class TestOpsState:
    def test_enum_values(self):
        from hft_platform.ops.ops_state_machine import OpsState
        assert OpsState.MAINTENANCE.value == "maintenance"
        assert OpsState.PRE_MARKET.value == "pre_market"
        assert OpsState.TRADING.value == "trading"
        assert OpsState.POST_MARKET.value == "post_market"
        assert OpsState.SETTLEMENT.value == "settlement"
        assert OpsState.NIGHT_SESSION.value == "night_session"


class TestOperationsStateMachine:
    def test_initial_state_is_maintenance(self):
        from hft_platform.ops.ops_state_machine import OperationsStateMachine
        sm = OperationsStateMachine(
            session_governor=MagicMock(), preflight_checker=MagicMock(), alert_callback=AsyncMock(),
        )
        from hft_platform.ops.ops_state_machine import OpsState
        assert sm.state == OpsState.MAINTENANCE

    @pytest.mark.asyncio
    async def test_transition_to_pre_market(self):
        from hft_platform.ops.ops_state_machine import OperationsStateMachine, OpsState
        alert_cb = AsyncMock()
        sm = OperationsStateMachine(
            session_governor=MagicMock(), preflight_checker=MagicMock(), alert_callback=alert_cb,
        )
        await sm.transition_to(OpsState.PRE_MARKET)
        assert sm.state == OpsState.PRE_MARKET
        alert_cb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_transition_emits_info_alert(self):
        from hft_platform.ops.ops_state_machine import OperationsStateMachine, OpsState
        alert_cb = AsyncMock()
        sm = OperationsStateMachine(
            session_governor=MagicMock(), preflight_checker=MagicMock(), alert_callback=alert_cb,
        )
        await sm.transition_to(OpsState.PRE_MARKET)
        alert = alert_cb.call_args.args[0]
        from hft_platform.notifications.alert import AlertSeverity
        assert alert.severity == AlertSeverity.INFO
        assert alert.category == "ops"

    @pytest.mark.asyncio
    async def test_pre_market_runs_preflight(self):
        from hft_platform.ops.ops_state_machine import OperationsStateMachine, OpsState
        from hft_platform.ops.preflight_checker import PreflightReport
        preflight = MagicMock()
        preflight.run_all = AsyncMock(return_value=PreflightReport(passed=True))
        sm = OperationsStateMachine(
            session_governor=MagicMock(), preflight_checker=preflight, alert_callback=AsyncMock(),
        )
        await sm.transition_to(OpsState.PRE_MARKET)
        result = await sm.run_preflight()
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_same_state_transition_is_noop(self):
        from hft_platform.ops.ops_state_machine import OperationsStateMachine, OpsState
        alert_cb = AsyncMock()
        sm = OperationsStateMachine(
            session_governor=MagicMock(), preflight_checker=MagicMock(), alert_callback=alert_cb,
        )
        await sm.transition_to(OpsState.PRE_MARKET)
        alert_cb.reset_mock()
        await sm.transition_to(OpsState.PRE_MARKET)
        alert_cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_state_history_recorded(self):
        from hft_platform.ops.ops_state_machine import OperationsStateMachine, OpsState
        sm = OperationsStateMachine(
            session_governor=MagicMock(), preflight_checker=MagicMock(), alert_callback=AsyncMock(),
        )
        await sm.transition_to(OpsState.PRE_MARKET)
        await sm.transition_to(OpsState.TRADING)
        assert len(sm.state_history) == 2
        assert sm.state_history[0][1] == OpsState.PRE_MARKET
        assert sm.state_history[1][1] == OpsState.TRADING

    @pytest.mark.asyncio
    async def test_register_callback_called_on_transition(self):
        from hft_platform.ops.ops_state_machine import OperationsStateMachine, OpsState
        cb = AsyncMock()
        sm = OperationsStateMachine(
            session_governor=MagicMock(), preflight_checker=MagicMock(), alert_callback=AsyncMock(),
        )
        sm.register_callback(cb)
        await sm.transition_to(OpsState.TRADING)
        cb.assert_awaited_once_with(OpsState.MAINTENANCE, OpsState.TRADING)

    @pytest.mark.asyncio
    async def test_callback_error_does_not_abort_transition(self):
        from hft_platform.ops.ops_state_machine import OperationsStateMachine, OpsState
        bad_cb = AsyncMock(side_effect=RuntimeError("boom"))
        sm = OperationsStateMachine(
            session_governor=MagicMock(), preflight_checker=MagicMock(), alert_callback=AsyncMock(),
        )
        sm.register_callback(bad_cb)
        # Should not raise — callback errors are swallowed with logging
        await sm.transition_to(OpsState.TRADING)
        assert sm.state == OpsState.TRADING

    @pytest.mark.asyncio
    async def test_full_lifecycle_transition_sequence(self):
        from hft_platform.ops.ops_state_machine import OperationsStateMachine, OpsState
        sm = OperationsStateMachine(
            session_governor=MagicMock(), preflight_checker=MagicMock(), alert_callback=AsyncMock(),
        )
        states = [
            OpsState.PRE_MARKET,
            OpsState.TRADING,
            OpsState.POST_MARKET,
            OpsState.SETTLEMENT,
            OpsState.MAINTENANCE,
        ]
        for target in states:
            await sm.transition_to(target)

        assert sm.state == OpsState.MAINTENANCE
        assert len(sm.state_history) == len(states)

    @pytest.mark.asyncio
    async def test_night_session_transition(self):
        from hft_platform.ops.ops_state_machine import OperationsStateMachine, OpsState
        sm = OperationsStateMachine(
            session_governor=MagicMock(), preflight_checker=MagicMock(), alert_callback=AsyncMock(),
        )
        await sm.transition_to(OpsState.NIGHT_SESSION)
        assert sm.state == OpsState.NIGHT_SESSION

    @pytest.mark.asyncio
    async def test_alert_metadata_contains_old_and_new_state(self):
        from hft_platform.ops.ops_state_machine import OperationsStateMachine, OpsState
        alert_cb = AsyncMock()
        sm = OperationsStateMachine(
            session_governor=MagicMock(), preflight_checker=MagicMock(), alert_callback=alert_cb,
        )
        await sm.transition_to(OpsState.TRADING)
        alert = alert_cb.call_args.args[0]
        assert alert.metadata["old_state"] == OpsState.MAINTENANCE.value
        assert alert.metadata["new_state"] == OpsState.TRADING.value

    @pytest.mark.asyncio
    async def test_alert_source_is_ops_state_machine(self):
        from hft_platform.ops.ops_state_machine import OperationsStateMachine, OpsState
        alert_cb = AsyncMock()
        sm = OperationsStateMachine(
            session_governor=MagicMock(), preflight_checker=MagicMock(), alert_callback=alert_cb,
        )
        await sm.transition_to(OpsState.PRE_MARKET)
        alert = alert_cb.call_args.args[0]
        assert alert.source == "ops_state_machine"

    def test_state_history_returns_copy(self):
        """Mutating returned history list must not affect internal state."""
        from hft_platform.ops.ops_state_machine import OperationsStateMachine
        sm = OperationsStateMachine(
            session_governor=MagicMock(), preflight_checker=MagicMock(), alert_callback=AsyncMock(),
        )
        history = sm.state_history
        history.append((999, "fake"))  # type: ignore[arg-type]
        assert len(sm.state_history) == 0
