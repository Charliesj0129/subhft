"""Coverage tests for AutonomyMonitor: lifecycle, execution, heartbeat, margin, flatten gate."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import StormGuardState
from hft_platform.ops.autonomy_monitor import (
    AutonomyMonitor,
    MonitorDecision,
    _handle_flatten_request,
)


def _make_monitor(**overrides) -> AutonomyMonitor:
    storm_guard = MagicMock()
    storm_guard.state = StormGuardState.NORMAL
    platform_degrade = MagicMock()
    platform_degrade.reduce_only_active = False
    platform_inputs = MagicMock()
    platform_inputs.reduce_only_reasons = MagicMock(return_value=[])

    kwargs = dict(
        storm_guard=storm_guard,
        platform_degrade=platform_degrade,
        platform_inputs=platform_inputs,
    )
    kwargs.update(overrides)
    return AutonomyMonitor(**kwargs)


# ---------------------------------------------------------------------------
# Lifecycle: run / start / stop
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_run_sets_running_and_loops(self) -> None:
        """run() sets _running=True and enters monitor loop (lines 139-141)."""
        monitor = _make_monitor(interval_s=0.01)

        async def _stop_after_tick():
            await asyncio.sleep(0.03)
            monitor._running = False

        stop_task = asyncio.create_task(_stop_after_tick())
        await monitor.run()
        await stop_task
        # run() exited cleanly
        assert monitor._running is False

    @pytest.mark.asyncio
    async def test_start_creates_task(self) -> None:
        """start() creates an internal asyncio.Task (lines 145-147)."""
        monitor = _make_monitor(interval_s=0.01)
        await monitor.start()
        assert monitor._task is not None
        assert isinstance(monitor._task, asyncio.Task)
        await monitor.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self) -> None:
        """stop() sets _running=False and cancels task (lines 151-158)."""
        monitor = _make_monitor(interval_s=0.01)
        await monitor.start()
        task = monitor._task
        await monitor.stop()
        assert monitor._running is False
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_stop_with_no_task(self) -> None:
        """stop() when no task was ever created does not raise."""
        monitor = _make_monitor()
        monitor._running = True
        monitor._task = None
        await monitor.stop()
        assert monitor._running is False


# ---------------------------------------------------------------------------
# Monitor loop: error handling, flatten gate, margin
# ---------------------------------------------------------------------------


class TestMonitorLoop:
    @pytest.mark.asyncio
    async def test_monitor_loop_catches_evaluate_exception(self) -> None:
        """Exception in _evaluate is caught and logged (lines 165-179)."""
        monitor = _make_monitor(interval_s=0.01)

        call_count = 0

        def exploding_evaluate(self_inner):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("evaluate boom")
            monitor._running = False
            return []

        with (
            patch.object(AutonomyMonitor, "_evaluate", exploding_evaluate),
            patch("hft_platform.ops.autonomy_monitor.asyncio.sleep", new=AsyncMock()),
        ):
            await monitor.run()
        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_monitor_loop_calls_flatten_gate_handler(self) -> None:
        """When flatten_gate and flattener are set, _handle_flatten_request is called (line 173)."""
        flatten_gate = MagicMock()
        flatten_gate.claim.return_value = None
        flattener = AsyncMock()
        monitor = _make_monitor(
            flatten_gate=flatten_gate,
            position_flattener=flattener,
            interval_s=0.01,
        )

        iterations = 0

        def counting_evaluate(self_inner):
            nonlocal iterations
            iterations += 1
            if iterations >= 2:
                monitor._running = False
            return []

        with (
            patch.object(AutonomyMonitor, "_evaluate", counting_evaluate),
            patch("hft_platform.ops.autonomy_monitor.asyncio.sleep", new=AsyncMock()),
        ):
            await monitor.run()
        assert flatten_gate.claim.call_count >= 1


# ---------------------------------------------------------------------------
# Margin checking (_check_margin)
# ---------------------------------------------------------------------------


class TestCheckMargin:
    @pytest.mark.asyncio
    async def test_check_margin_skips_when_no_monitor(self) -> None:
        """_check_margin returns early if margin_monitor is None (line 187-188)."""
        monitor = _make_monitor(margin_monitor=None)
        # Should not raise
        await monitor._check_margin()

    @pytest.mark.asyncio
    async def test_check_margin_skips_when_result_is_none(self) -> None:
        """_check_margin returns early if check returns None (lines 190-193)."""
        margin_mon = AsyncMock()
        margin_mon.check = AsyncMock(return_value=None)
        monitor = _make_monitor(margin_monitor=margin_mon)
        await monitor._check_margin()
        margin_mon.check.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_check_margin_critical_enters_reduce_only(self) -> None:
        """Critical margin triggers enter_reduce_only (lines 195-196, 199-200)."""
        result = SimpleNamespace(action="critical", ratio=0.95, margin_used=950, margin_available=1000)
        margin_mon = AsyncMock()
        margin_mon.check = AsyncMock(return_value=result)
        notifier = AsyncMock()
        monitor = _make_monitor(margin_monitor=margin_mon, notification_dispatcher=notifier)
        await monitor._check_margin()
        monitor._platform_degrade.enter_reduce_only.assert_called_once()
        notifier.notify_margin_critical.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_check_margin_critical_without_notifier(self) -> None:
        """Critical margin without notification_dispatcher still enters reduce_only."""
        result = SimpleNamespace(action="critical", ratio=0.95, margin_used=950, margin_available=1000)
        margin_mon = AsyncMock()
        margin_mon.check = AsyncMock(return_value=result)
        monitor = _make_monitor(margin_monitor=margin_mon, notification_dispatcher=None)
        await monitor._check_margin()
        monitor._platform_degrade.enter_reduce_only.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_margin_warn_notifies(self) -> None:
        """Warn margin triggers notification (lines 205-207)."""
        result = SimpleNamespace(action="warn", ratio=0.85, margin_used=850, margin_available=1000)
        margin_mon = AsyncMock()
        margin_mon.check = AsyncMock(return_value=result)
        notifier = AsyncMock()
        monitor = _make_monitor(margin_monitor=margin_mon, notification_dispatcher=notifier)
        await monitor._check_margin()
        notifier.notify_margin_warning.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_check_margin_warn_without_notifier(self) -> None:
        """Warn margin without notifier does not crash."""
        result = SimpleNamespace(action="warn", ratio=0.85, margin_used=850, margin_available=1000)
        margin_mon = AsyncMock()
        margin_mon.check = AsyncMock(return_value=result)
        monitor = _make_monitor(margin_monitor=margin_mon, notification_dispatcher=None)
        await monitor._check_margin()
        # Should not raise -- no assertion needed beyond no exception


# ---------------------------------------------------------------------------
# Evaluation: infra reasons, recon drift
# ---------------------------------------------------------------------------


class TestEvaluateInfra:
    def test_platform_inputs_exception_yields_empty_reasons(self) -> None:
        """Exception from platform_inputs.reduce_only_reasons is caught (lines 306-307)."""
        monitor = _make_monitor()
        monitor._platform_inputs.reduce_only_reasons.side_effect = RuntimeError("inputs boom")
        decisions = monitor._evaluate()
        assert decisions == []

    def test_reconciliation_drift_triggers_reduce_only(self) -> None:
        """drift_streak >= 2 triggers reconciliation_drift decision (lines 351-356)."""
        recon = MagicMock()
        recon.drift_streak = 3
        monitor = _make_monitor(recon_service=recon)
        decisions = monitor._evaluate()
        drift_decisions = [d for d in decisions if d.rule_name == "reconciliation_drift"]
        assert len(drift_decisions) == 1
        assert drift_decisions[0].action == "enter_reduce_only"

    def test_reconciliation_drift_below_threshold_no_decision(self) -> None:
        """drift_streak < 2 produces no decision."""
        recon = MagicMock()
        recon.drift_streak = 1
        monitor = _make_monitor(recon_service=recon)
        decisions = monitor._evaluate()
        drift_decisions = [d for d in decisions if d.rule_name == "reconciliation_drift"]
        assert len(drift_decisions) == 0

    def test_reconciliation_drift_exception_caught(self) -> None:
        """Exception reading drift_streak is caught (lines 351-354)."""
        recon = MagicMock()
        type(recon).drift_streak = property(lambda self: (_ for _ in ()).throw(RuntimeError("drift boom")))
        monitor = _make_monitor(recon_service=recon)
        decisions = monitor._evaluate()
        # Should not crash, drift defaults to 0
        assert isinstance(decisions, list)

    def test_recorder_data_loss_triggers_halt(self) -> None:
        """recorder_data_loss in reasons triggers HALT via storm_guard."""
        monitor = _make_monitor()
        monitor._platform_inputs.reduce_only_reasons.return_value = ["recorder_data_loss"]
        decisions = monitor._evaluate()
        monitor._storm_guard.trigger_halt.assert_called_once_with("recorder_data_loss")
        halt_decisions = [d for d in decisions if d.rule_name == "recorder_data_loss"]
        assert len(halt_decisions) == 1
        assert halt_decisions[0].action == "trigger_halt"


# ---------------------------------------------------------------------------
# Execution: flatten success, enter_reduce_only, evidence, notifications
# ---------------------------------------------------------------------------


class TestExecute:
    @pytest.mark.asyncio
    async def test_flatten_success_with_notifier(self) -> None:
        """Successful flatten_all with notification_dispatcher (lines 385-389)."""
        flattener = AsyncMock()
        flattener.flatten_all.return_value = SimpleNamespace(
            fully_closed=2, partially_closed=1, failed=0, failed_symbols=[]
        )
        notifier = AsyncMock()
        monitor = _make_monitor(position_flattener=flattener, notification_dispatcher=notifier)
        decisions = [
            MonitorDecision(
                rule_name="halt_reaction",
                action="flatten_all",
                reason="stormguard_halt",
                scope="platform",
                rearm="manual",
            )
        ]
        await monitor._execute(decisions)
        notifier.notify_flatten_result.assert_awaited_once()
        assert monitor._halt_reacted is True

    @pytest.mark.asyncio
    async def test_flatten_exhausted_notifies(self) -> None:
        """Max retries exhausted sends FLATTEN_EXHAUSTED notification (lines 413-422)."""
        flattener = AsyncMock()
        flattener.flatten_all.side_effect = RuntimeError("broker down")
        notifier = AsyncMock()
        monitor = _make_monitor(position_flattener=flattener, notification_dispatcher=notifier)
        monitor._halt_flatten_attempts = 2  # one more will hit max_retries=3
        decisions = [
            MonitorDecision(
                rule_name="halt_reaction",
                action="flatten_all",
                reason="stormguard_halt",
                scope="platform",
                rearm="manual",
            )
        ]
        await monitor._execute(decisions)
        assert monitor._halt_reacted is True
        notifier.notify_flatten_result.assert_awaited_once()
        call_kwargs = notifier.notify_flatten_result.call_args.kwargs
        assert call_kwargs["failed_symbols"] == ["FLATTEN_EXHAUSTED"]

    @pytest.mark.asyncio
    async def test_flatten_exhausted_notifier_exception_caught(self) -> None:
        """Exception in notify_flatten_result during exhaustion is caught (lines 421-422)."""
        flattener = AsyncMock()
        flattener.flatten_all.side_effect = RuntimeError("broker down")
        notifier = AsyncMock()
        notifier.notify_flatten_result.side_effect = RuntimeError("notify boom")
        monitor = _make_monitor(position_flattener=flattener, notification_dispatcher=notifier)
        monitor._halt_flatten_attempts = 2
        decisions = [
            MonitorDecision(
                rule_name="halt_reaction",
                action="flatten_all",
                reason="stormguard_halt",
                scope="platform",
                rearm="manual",
            )
        ]
        # Should not raise
        await monitor._execute(decisions)
        assert monitor._halt_reacted is True

    @pytest.mark.asyncio
    async def test_enter_reduce_only_execution(self) -> None:
        """enter_reduce_only decision calls platform_degrade (lines 424-428)."""
        monitor = _make_monitor()
        decisions = [
            MonitorDecision(
                rule_name="rss_unhealthy",
                action="enter_reduce_only",
                reason="rss_unhealthy",
                scope="platform",
                rearm="manual",
            )
        ]
        await monitor._execute(decisions)
        monitor._platform_degrade.enter_reduce_only.assert_called_once_with(reason="rss_unhealthy")

    @pytest.mark.asyncio
    async def test_enter_reduce_only_exception_caught(self) -> None:
        """Exception in enter_reduce_only is caught (lines 427-428)."""
        monitor = _make_monitor()
        monitor._platform_degrade.enter_reduce_only.side_effect = RuntimeError("degrade boom")
        decisions = [
            MonitorDecision(
                rule_name="rss_unhealthy",
                action="enter_reduce_only",
                reason="rss_unhealthy",
                scope="platform",
                rearm="manual",
            )
        ]
        # Should not raise
        await monitor._execute(decisions)

    @pytest.mark.asyncio
    async def test_evidence_writer_records_transition(self) -> None:
        """Evidence writer is called for each decision (lines 432-433)."""
        evidence = MagicMock()
        monitor = _make_monitor(evidence_writer=evidence)
        decisions = [
            MonitorDecision(
                rule_name="rss_unhealthy",
                action="enter_reduce_only",
                reason="rss_unhealthy",
                scope="platform",
                rearm="manual",
            )
        ]
        await monitor._execute(decisions)
        evidence.record_transition.assert_called_once_with(
            scope="platform",
            mode="enter_reduce_only",
            reason="rss_unhealthy",
            manual_rearm_required=True,
        )

    @pytest.mark.asyncio
    async def test_evidence_writer_exception_caught(self) -> None:
        """Exception in evidence_writer.record_transition is caught (lines 439-440)."""
        evidence = MagicMock()
        evidence.record_transition.side_effect = RuntimeError("write boom")
        monitor = _make_monitor(evidence_writer=evidence)
        decisions = [
            MonitorDecision(
                rule_name="rss_unhealthy",
                action="enter_reduce_only",
                reason="rss_unhealthy",
                scope="platform",
                rearm="manual",
            )
        ]
        # Should not raise
        await monitor._execute(decisions)


# ---------------------------------------------------------------------------
# Heartbeat (_maybe_heartbeat)
# ---------------------------------------------------------------------------


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_skips_when_no_dispatcher(self) -> None:
        """No notification_dispatcher means no heartbeat (lines 463-464)."""
        monitor = _make_monitor(notification_dispatcher=None)
        await monitor._maybe_heartbeat()
        # No crash, no action

    @pytest.mark.asyncio
    async def test_heartbeat_skips_when_interval_not_elapsed(self) -> None:
        """Heartbeat only fires after heartbeat_interval_s (lines 465-467)."""
        notifier = AsyncMock()
        monitor = _make_monitor(notification_dispatcher=notifier, heartbeat_interval_s=1800.0)
        # Simulate recent heartbeat
        with patch("hft_platform.ops.autonomy_monitor.timebase") as tb:
            tb.now_ns.return_value = 100_000_000_000
            monitor._last_heartbeat_ns = 100_000_000_000 - 1_000_000_000  # 1s ago
            await monitor._maybe_heartbeat()
        notifier.notify_heartbeat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_heartbeat_fires_when_interval_elapsed(self) -> None:
        """Heartbeat fires after interval elapses (lines 468-478)."""
        notifier = AsyncMock()
        monitor = _make_monitor(notification_dispatcher=notifier, heartbeat_interval_s=1.0)
        with patch("hft_platform.ops.autonomy_monitor.timebase") as tb:
            tb.now_ns.return_value = 100_000_000_000
            monitor._last_heartbeat_ns = 0
            await monitor._maybe_heartbeat()
        notifier.notify_heartbeat.assert_awaited_once()
        call_kwargs = notifier.notify_heartbeat.call_args.kwargs
        assert "autonomy_state" in call_kwargs
        assert "feed_status" in call_kwargs

    @pytest.mark.asyncio
    async def test_heartbeat_reads_pnl_from_position_store(self) -> None:
        """Heartbeat reads pnl from storm_guard.position_store (lines 471-472)."""
        notifier = AsyncMock()
        monitor = _make_monitor(notification_dispatcher=notifier, heartbeat_interval_s=0.001)
        monitor._storm_guard.position_store = MagicMock(total_pnl=42000)
        with patch("hft_platform.ops.autonomy_monitor.timebase") as tb:
            tb.now_ns.return_value = 100_000_000_000
            monitor._last_heartbeat_ns = 0
            await monitor._maybe_heartbeat()
        call_kwargs = notifier.notify_heartbeat.call_args.kwargs
        assert call_kwargs["pnl_scaled"] == 42000

    @pytest.mark.asyncio
    async def test_heartbeat_exception_caught(self) -> None:
        """Exception in notify_heartbeat is caught (lines 479-480)."""
        notifier = AsyncMock()
        notifier.notify_heartbeat.side_effect = RuntimeError("heartbeat boom")
        monitor = _make_monitor(notification_dispatcher=notifier, heartbeat_interval_s=0.001)
        with patch("hft_platform.ops.autonomy_monitor.timebase") as tb:
            tb.now_ns.return_value = 100_000_000_000
            monitor._last_heartbeat_ns = 0
            await monitor._maybe_heartbeat()
        # Should not raise

    @pytest.mark.asyncio
    async def test_heartbeat_feed_status_disconnected(self) -> None:
        """Heartbeat reports 'disconnected' when broker was disconnected."""
        notifier = AsyncMock()
        monitor = _make_monitor(notification_dispatcher=notifier, heartbeat_interval_s=0.001)
        monitor._broker_was_connected = False
        with patch("hft_platform.ops.autonomy_monitor.timebase") as tb:
            tb.now_ns.return_value = 100_000_000_000
            monitor._last_heartbeat_ns = 0
            await monitor._maybe_heartbeat()
        call_kwargs = notifier.notify_heartbeat.call_args.kwargs
        assert call_kwargs["feed_status"] == "disconnected"


# ---------------------------------------------------------------------------
# _handle_flatten_request (module-level helper)
# ---------------------------------------------------------------------------


class TestHandleFlattenRequest:
    @pytest.mark.asyncio
    async def test_no_pending_request_returns_early(self) -> None:
        """claim() returns None => no flattening (line 491)."""
        gate = MagicMock()
        gate.claim.return_value = None
        flattener = AsyncMock()
        await _handle_flatten_request(gate, flattener)
        flattener.flatten_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_flatten_all_on_scope_all(self) -> None:
        """scope='all' calls flatten_all and completes gate (lines 499-507)."""
        gate = MagicMock()
        gate.claim.return_value = SimpleNamespace(scope="all", scope_id=None)
        flattener = AsyncMock()
        flattener.flatten_all.return_value = SimpleNamespace(
            fully_closed=3, partially_closed=0, failed=0, failed_symbols=[]
        )
        await _handle_flatten_request(gate, flattener)
        flattener.flatten_all.assert_awaited_once()
        gate.complete.assert_called_once_with(fully_closed=3, partially_closed=0, failed=0, failed_symbols=[])

    @pytest.mark.asyncio
    async def test_flatten_track_scope(self) -> None:
        """scope='track' with scope_id calls flatten_track (lines 495-496)."""
        gate = MagicMock()
        gate.claim.return_value = SimpleNamespace(scope="track", scope_id="track_A")
        flattener = AsyncMock()
        flattener.flatten_track.return_value = SimpleNamespace(
            fully_closed=1, partially_closed=0, failed=0, failed_symbols=[]
        )
        await _handle_flatten_request(gate, flattener)
        flattener.flatten_track.assert_awaited_once_with("track_A", [])

    @pytest.mark.asyncio
    async def test_flatten_strategy_scope(self) -> None:
        """scope='strategy' with scope_id calls flatten_strategy (lines 497-498)."""
        gate = MagicMock()
        gate.claim.return_value = SimpleNamespace(scope="strategy", scope_id="strat_1")
        flattener = AsyncMock()
        flattener.flatten_strategy.return_value = SimpleNamespace(
            fully_closed=2, partially_closed=0, failed=0, failed_symbols=[]
        )
        await _handle_flatten_request(gate, flattener)
        flattener.flatten_strategy.assert_awaited_once_with("strat_1")

    @pytest.mark.asyncio
    async def test_flatten_request_exception_calls_fail(self) -> None:
        """Exception in flatten operation calls gate.fail (lines 514-516)."""
        gate = MagicMock()
        gate.claim.return_value = SimpleNamespace(scope="all", scope_id=None)
        flattener = AsyncMock()
        flattener.flatten_all.side_effect = RuntimeError("flatten exploded")
        await _handle_flatten_request(gate, flattener)
        gate.fail.assert_called_once()
        assert "flatten exploded" in gate.fail.call_args[0][0]

    @pytest.mark.asyncio
    async def test_flatten_track_without_scope_id_falls_to_all(self) -> None:
        """scope='track' but scope_id=None falls through to flatten_all."""
        gate = MagicMock()
        gate.claim.return_value = SimpleNamespace(scope="track", scope_id=None)
        flattener = AsyncMock()
        flattener.flatten_all.return_value = SimpleNamespace(
            fully_closed=1, partially_closed=0, failed=0, failed_symbols=[]
        )
        await _handle_flatten_request(gate, flattener)
        flattener.flatten_all.assert_awaited_once()


# ---------------------------------------------------------------------------
# Broker disconnect edge cases
# ---------------------------------------------------------------------------


class TestBrokerDisconnectEdgeCases:
    def test_broker_disconnect_first_detection_records_since(self) -> None:
        """First disconnect detection sets _broker_disconnect_since_ns (lines 223-224, 228-229)."""
        broker = MagicMock()
        broker.is_connected.return_value = False
        monitor = _make_monitor(broker_client=broker)
        monitor._broker_was_connected = True  # was connected before

        decisions: list[MonitorDecision] = []
        now_ns = 100_000_000_000
        monitor._check_broker_disconnect(decisions, now_ns)
        assert monitor._broker_was_connected is False
        assert monitor._broker_disconnect_since_ns == now_ns

    def test_broker_reconnect_clears_state(self) -> None:
        """Broker reconnecting resets disconnect tracking."""
        broker = MagicMock()
        broker.is_connected.return_value = True
        monitor = _make_monitor(broker_client=broker)
        monitor._broker_was_connected = False
        monitor._broker_disconnect_since_ns = 100_000_000_000

        decisions: list[MonitorDecision] = []
        monitor._check_broker_disconnect(decisions, 200_000_000_000)
        assert monitor._broker_was_connected is True
        assert monitor._broker_disconnect_since_ns == 0
        assert len(decisions) == 0

    def test_broker_is_connected_exception_treated_as_disconnected(self) -> None:
        """Exception in is_connected() is treated as disconnected."""
        broker = MagicMock()
        broker.is_connected.side_effect = RuntimeError("conn check boom")
        monitor = _make_monitor(broker_client=broker)
        monitor._broker_was_connected = False
        monitor._broker_disconnect_since_ns = 0

        decisions: list[MonitorDecision] = []
        now_ns = 999_999_999_999
        monitor._check_broker_disconnect(decisions, now_ns)
        # Exception in is_connected => connected=False => records as disconnect
        assert len(decisions) == 1
        assert decisions[0].reason == "broker_unavailable"
