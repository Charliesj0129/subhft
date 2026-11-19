"""Tests for AutonomyMonitor: HALT reaction, normal no decisions, cooldown, broker disconnect."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import StormGuardState
from hft_platform.ops.autonomy_monitor import AutonomyMonitor, MonitorDecision
from hft_platform.ops.evidence import AutonomyEvidenceWriter


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


class TestHaltReaction:
    def test_halt_produces_flatten_decision(self) -> None:
        monitor = _make_monitor()
        monitor._storm_guard.state = StormGuardState.HALT
        decisions = monitor._evaluate()
        assert len(decisions) == 1
        assert decisions[0].action == "flatten_all"
        assert decisions[0].reason == "stormguard_halt"

    def test_halt_only_reacts_once(self) -> None:
        monitor = _make_monitor()
        monitor._storm_guard.state = StormGuardState.HALT
        decisions1 = monitor._evaluate()
        assert len(decisions1) == 1
        monitor._halt_reacted = True
        decisions2 = monitor._evaluate()
        assert len(decisions2) == 0


class TestNormalNoDecisions:
    def test_normal_state_no_decisions(self) -> None:
        monitor = _make_monitor()
        decisions = monitor._evaluate()
        assert decisions == []

    def test_warm_state_no_decisions(self) -> None:
        monitor = _make_monitor()
        monitor._storm_guard.state = StormGuardState.WARM
        decisions = monitor._evaluate()
        assert decisions == []


class TestCooldown:
    def test_cooldown_prevents_duplicate_decisions(self) -> None:
        monitor = _make_monitor()
        monitor._platform_inputs.reduce_only_reasons.return_value = ["rss_unhealthy"]
        decisions1 = monitor._evaluate()
        assert len(decisions1) == 1
        monitor._apply_cooldowns(decisions1)
        decisions2 = monitor._evaluate()
        assert len(decisions2) == 0


class TestBrokerDisconnect:
    def test_broker_disconnect_over_threshold_triggers_reduce_only(self) -> None:
        broker = MagicMock()
        broker.is_connected = MagicMock(return_value=False)
        monitor = _make_monitor(broker_client=broker)

        monitor._broker_was_connected = False
        monitor._broker_disconnect_since_ns = 0

        decisions: list[MonitorDecision] = []
        now_ns = 999_999_999_999
        monitor._check_broker_disconnect(decisions, now_ns)
        assert len(decisions) == 1
        assert decisions[0].reason == "broker_unavailable"

    def test_connected_broker_no_decisions(self) -> None:
        broker = MagicMock()
        broker.is_connected = MagicMock(return_value=True)
        monitor = _make_monitor(broker_client=broker)
        decisions: list[MonitorDecision] = []
        monitor._check_broker_disconnect(decisions, 1_000_000_000)
        assert decisions == []

    def test_reduce_only_still_checks_broker_disconnect(self) -> None:
        """M15: broker disconnect check runs even during reduce_only."""
        broker = MagicMock()
        broker.is_connected = MagicMock(return_value=False)
        monitor = _make_monitor(broker_client=broker)
        monitor._platform_degrade.reduce_only_active = True
        monitor._broker_was_connected = False
        monitor._broker_disconnect_since_ns = 0

        now_ns = 999_999_999_999
        with patch("hft_platform.ops.autonomy_monitor.timebase") as mock_tb:
            mock_tb.now_ns.return_value = now_ns
            decisions = monitor._evaluate()

        broker_decisions = [d for d in decisions if d.reason == "broker_unavailable"]
        assert len(broker_decisions) >= 1


class TestHaltFlattenRetry:
    """Tests for C2: HALT flatten retry cap and H2: cooldown-guarded reset."""

    @pytest.mark.asyncio
    async def test_halt_flatten_failure_retries_up_to_max(self) -> None:
        flattener = AsyncMock()
        flattener.flatten_all.side_effect = RuntimeError("broker down")
        monitor = _make_monitor(position_flattener=flattener)
        monitor._storm_guard.state = StormGuardState.HALT

        base_ns = 1_000_000_000_000
        tick_ns = 3_600_000_000_000
        call_count = 0
        with patch("hft_platform.ops.autonomy_monitor.timebase") as mock_tb:
            mock_tb.now_ns.return_value = base_ns
            for i in range(5):
                mock_tb.now_ns.return_value = base_ns + i * tick_ns
                decisions = monitor._evaluate()
                if decisions:
                    await monitor._execute(decisions)
                    call_count += 1

        assert flattener.flatten_all.call_count == 3
        assert call_count == 3
        assert monitor._halt_flatten_attempts == 3

    @pytest.mark.asyncio
    async def test_halt_flatten_failure_sets_reacted_after_max_retries(self) -> None:
        flattener = AsyncMock()
        flattener.flatten_all.side_effect = RuntimeError("broker down")
        monitor = _make_monitor(position_flattener=flattener)
        monitor._storm_guard.state = StormGuardState.HALT

        base_ns = 1_000_000_000_000
        tick_ns = 3_600_000_000_000
        with patch("hft_platform.ops.autonomy_monitor.timebase") as mock_tb:
            for i in range(3):
                mock_tb.now_ns.return_value = base_ns + i * tick_ns
                decisions = monitor._evaluate()
                if decisions:
                    await monitor._execute(decisions)

        assert monitor._halt_reacted is True
        assert monitor._halt_reacted_ns > 0

    @patch("hft_platform.ops.autonomy_monitor.timebase")
    def test_halt_reacted_reset_requires_cooldown(self, mock_tb: MagicMock) -> None:
        monitor = _make_monitor()
        monitor._halt_reacted = True
        monitor._halt_reacted_ns = 100_000_000_000

        mock_tb.now_ns.return_value = 130_000_000_000
        monitor._storm_guard.state = StormGuardState.NORMAL
        monitor._evaluate()
        assert monitor._halt_reacted is True

        mock_tb.now_ns.return_value = 161_000_000_000
        monitor._evaluate()
        assert monitor._halt_reacted is False
        assert monitor._halt_flatten_attempts == 0

    @pytest.mark.asyncio
    @patch("hft_platform.ops.autonomy_monitor.timebase")
    async def test_halt_flatten_backoff_between_retries(self, mock_tb: MagicMock) -> None:
        """Retries use exponential backoff: 5s, 10s, 20s."""
        flattener = MagicMock()
        flattener.flatten_all = AsyncMock(side_effect=RuntimeError("broker down"))
        monitor = _make_monitor(position_flattener=flattener)
        monitor._storm_guard.state = StormGuardState.HALT

        current_time = 1_000_000_000_000

        mock_tb.now_ns.return_value = current_time
        decisions = monitor._evaluate()
        assert len(decisions) == 1
        await monitor._execute(decisions)
        assert flattener.flatten_all.call_count == 1
        assert monitor._halt_next_retry_ns > current_time

        current_time += 3_000_000_000
        mock_tb.now_ns.return_value = current_time
        decisions = monitor._evaluate()
        assert len(decisions) == 0

        current_time += 3_000_000_000
        mock_tb.now_ns.return_value = current_time
        decisions = monitor._evaluate()
        assert len(decisions) == 1
        await monitor._execute(decisions)
        assert flattener.flatten_all.call_count == 2

    @patch("hft_platform.ops.autonomy_monitor.timebase")
    def test_halt_oscillation_no_double_flatten(self, mock_tb: MagicMock) -> None:
        monitor = _make_monitor(position_flattener=AsyncMock())
        mock_tb.now_ns.return_value = 100_000_000_000
        monitor._storm_guard.state = StormGuardState.HALT
        decisions1 = monitor._evaluate()
        assert len(decisions1) == 1
        assert decisions1[0].action == "flatten_all"

        monitor._halt_reacted = True
        monitor._halt_reacted_ns = 100_000_000_000

        mock_tb.now_ns.return_value = 110_000_000_000
        monitor._storm_guard.state = StormGuardState.WARM
        monitor._evaluate()
        assert monitor._halt_reacted is True

        mock_tb.now_ns.return_value = 120_000_000_000
        monitor._storm_guard.state = StormGuardState.HALT
        decisions2 = monitor._evaluate()
        assert len(decisions2) == 0


class TestTheEvidenceWriteDoesNotRunOnTheEventLoop:
    """``record_transition`` acquires a ``threading.Lock`` across a multi-file write.

    One of those files goes through ``locked_state``, which acquires an
    ``flock`` by polling with ``time.sleep(0.01)`` up to a two-second deadline.
    That lock used to have exactly one holder -- this thread -- so calling it
    inline was free. ``StrategyHealthGovernor.quarantine_async`` gave it a
    second holder on a worker thread, and ``threading.Lock.acquire`` is not
    awaitable: a loop-side call now stops market, risk and order processing for
    as long as the worker keeps the lock.
    """

    def test_the_evidence_write_happens_on_a_worker_thread(self, tmp_path) -> None:
        import asyncio
        import threading

        monitor = _make_monitor()
        writer = AutonomyEvidenceWriter(tmp_path)
        seen: dict[str, int] = {}

        def _record(**_kwargs) -> None:
            seen["thread"] = threading.get_ident()

        writer.record_transition = _record  # type: ignore[method-assign]
        monitor._evidence_writer = writer
        decision = MonitorDecision(
            rule_name="r",
            action="notify_quarantine",
            reason="test",
            scope="platform",
            rearm="manual",
        )

        async def _run() -> int:
            await monitor._execute([decision])
            return threading.get_ident()

        loop_thread = asyncio.run(_run())
        assert "thread" in seen, "the evidence write never ran"
        assert seen["thread"] != loop_thread, "the evidence write ran on the event loop thread"

    def test_a_held_evidence_lock_does_not_stall_the_loop(self, tmp_path) -> None:
        """The loop must stay runnable while another holder keeps the lock."""
        import asyncio
        import threading

        monitor = _make_monitor()
        writer = AutonomyEvidenceWriter(tmp_path)
        release = threading.Event()
        entered = threading.Event()

        def _record(**_kwargs) -> None:
            entered.set()
            release.wait(timeout=5.0)

        writer.record_transition = _record  # type: ignore[method-assign]
        monitor._evidence_writer = writer
        decision = MonitorDecision(
            rule_name="r",
            action="notify_quarantine",
            reason="test",
            scope="platform",
            rearm="manual",
        )

        ticks = 0

        async def _ticker() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.005)

        async def _run() -> None:
            t = asyncio.ensure_future(_ticker())
            exec_task = asyncio.ensure_future(monitor._execute([decision]))
            await asyncio.sleep(0.15)
            assert entered.is_set(), "the evidence write never started"
            release.set()
            await exec_task
            t.cancel()

        asyncio.run(_run())
        assert ticks > 5, f"the loop only advanced {ticks} times; it was blocked"
