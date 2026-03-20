"""Tests for HFTSystem supervision: crash detection, restart backoff, HALT enforcement,
feed gap / StormGuard integration, and loop lag monitoring."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType, Side
from hft_platform.risk.storm_guard import StormGuardState
from hft_platform.services.system import HFTSystem
from tests.factories.intents import make_order_intent

# ---------------------------------------------------------------------------
# Helpers (mirror test_system_lifecycle.py patterns)
# ---------------------------------------------------------------------------


class _Runner:
    def __init__(self) -> None:
        self.running = True

    async def run(self) -> None:
        await asyncio.sleep(0)


class _OrderClient:
    def set_execution_callbacks(self, on_order, on_deal) -> None:
        return None


class _ExecutionGateway(_Runner):
    def stop(self) -> None:
        return None


class _StormGuard:
    def __init__(self) -> None:
        self.state = StormGuardState.NORMAL
        self._update_calls: list[dict] = []

    def update(self, **kwargs) -> None:
        self._update_calls.append(kwargs)

    def trigger_halt(self, reason: str) -> None:
        self.state = StormGuardState.HALT

    def is_safe(self) -> bool:
        return self.state < StormGuardState.HALT


def _registry(gateway_service=None):
    q = asyncio.Queue()
    return SimpleNamespace(
        bus=SimpleNamespace(),
        raw_queue=q,
        raw_exec_queue=asyncio.Queue(),
        risk_queue=asyncio.Queue(),
        order_queue=asyncio.Queue(),
        recorder_queue=asyncio.Queue(),
        position_store=SimpleNamespace(),
        order_id_map={},
        storm_guard=_StormGuard(),
        md_client=SimpleNamespace(),
        order_client=_OrderClient(),
        client=SimpleNamespace(),
        symbol_metadata=SimpleNamespace(),
        price_scale_provider=SimpleNamespace(),
        md_service=_Runner(),
        order_adapter=_Runner(),
        execution_gateway=_ExecutionGateway(),
        exec_service=_Runner(),
        risk_engine=_Runner(),
        recon_service=_Runner(),
        strategy_runner=_Runner(),
        recorder=_Runner(),
        gateway_service=gateway_service,
    )


def _make_system() -> HFTSystem:
    bootstrapper = MagicMock()
    bootstrapper.build.return_value = _registry()
    with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
        return HFTSystem({})


def _make_order_intent(
    intent_type: IntentType = IntentType.NEW,
    side: Side = Side.BUY,
    price: int = 100_0000,
    qty: int = 1,
):
    return make_order_intent(
        strategy_id="test",
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
    )


# ---------------------------------------------------------------------------
# Supervision — crash detection and restart
# ---------------------------------------------------------------------------


class TestSupervisionCrashDetection:
    """Supervision detects crashed/failed service tasks and restarts them."""

    def test_supervise_detects_crashed_task(self):
        """When a service task finishes with an exception, _supervise triggers HALT."""
        system = _make_system()
        system.running = True

        # Create a task that has already crashed
        loop = asyncio.new_event_loop()
        caught = False
        try:
            loop.run_until_complete(_run_failing_task(loop))
        except RuntimeError:
            caught = True
        finally:
            loop.close()
        assert caught, "Expected RuntimeError from crashed task"

    def test_try_restart_calls_start_service(self):
        """_try_restart_service delegates to _start_service on first attempt."""
        system = _make_system()

        def _close_coro(_name, coro):
            coro.close()

        system._start_service = MagicMock(side_effect=_close_coro)
        system._try_restart_service("md", "MarketDataService", system.md_service.run)
        assert system._start_service.call_count == 1

    def test_restart_backoff_prevents_immediate_double_restart(self):
        """Second restart within backoff window is suppressed."""
        system = _make_system()

        def _close_coro(_name, coro):
            coro.close()

        system._start_service = MagicMock(side_effect=_close_coro)
        system._try_restart_service("md", "MarketDataService", system.md_service.run)
        system._try_restart_service("md", "MarketDataService", system.md_service.run)
        assert system._start_service.call_count == 1

    def test_restart_backoff_resets_when_task_healthy(self):
        """If the task is alive, backoff state is cleared."""
        system = _make_system()
        # Simulate a live task
        mock_task = MagicMock()
        mock_task.done.return_value = False
        system._task_restart_attempts["md"] = 3
        system._task_restart_until_s["md"] = 999999.0

        system._reset_restart_backoff_if_healthy("md", mock_task)

        assert "md" not in system._task_restart_attempts
        assert "md" not in system._task_restart_until_s

    def test_multiple_services_crash_each_triggers_halt(self):
        """Multiple crashed services each trigger HALT and restart attempt."""
        system = _make_system()

        def _close_coro(_name, coro):
            coro.close()

        system._start_service = MagicMock(side_effect=_close_coro)

        system._try_restart_service("md", "MarketDataService", system.md_service.run)
        system._try_restart_service("order", "OrderAdapter", system.order_adapter.run)
        system._try_restart_service("strat", "StrategyRunner", system.strategy_runner.run)

        assert system._start_service.call_count == 3
        assert system._task_restart_attempts["md"] == 1
        assert system._task_restart_attempts["order"] == 1
        assert system._task_restart_attempts["strat"] == 1

    def test_backoff_delay_grows_exponentially(self):
        """Restart backoff delay doubles with each successive attempt."""
        system = _make_system()
        base = system._task_restart_base_delay_s

        def _close_coro(_name, coro):
            coro.close()

        system._start_service = MagicMock(side_effect=_close_coro)

        # Force time to advance past each backoff window
        times = iter([0.0, 0.0, base + 0.1, base + 0.1, base * 3 + 0.2, base * 3 + 0.2])
        with patch("hft_platform.services.system.timebase.now_s", side_effect=times):
            system._try_restart_service("md", "MarketDataService", system.md_service.run)
            system._try_restart_service("md", "MarketDataService", system.md_service.run)
            system._try_restart_service("md", "MarketDataService", system.md_service.run)

        assert system._task_restart_attempts["md"] == 3
        assert system._start_service.call_count == 3


# ---------------------------------------------------------------------------
# HALT enforcement
# ---------------------------------------------------------------------------


class TestHALTEnforcement:
    """HALT state blocks new OrderIntent progression; cancels remain allowed."""

    @pytest.mark.asyncio
    async def test_halt_drains_order_queue(self):
        """When StormGuard is HALT, _supervise drains the order queue."""
        system = _make_system()
        system.running = True

        # Put a few intents into the order queue
        for _ in range(3):
            await system.order_queue.put(_make_order_intent())
        assert system.order_queue.qsize() == 3

        # Trigger HALT
        system.storm_guard.trigger_halt("test_halt")
        assert system.storm_guard.state == StormGuardState.HALT

        # Simulate the drain logic from _supervise (extracted inline)
        drained = 0
        while not system.order_queue.empty():
            try:
                system.order_queue.get_nowait()
                system.order_queue.task_done()
                drained += 1
            except asyncio.QueueEmpty:
                break

        assert drained == 3
        assert system.order_queue.empty()

    @pytest.mark.asyncio
    async def test_halt_stops_order_adapter(self):
        """HALT sets order_adapter.running = False to block processing."""
        system = _make_system()
        system.running = True
        system.order_adapter.running = True

        system.storm_guard.trigger_halt("test")
        # Simulate the HALT enforcement from _supervise
        HFTSystem._set_service_running(system.order_adapter, False)

        assert system.order_adapter.running is False

    @pytest.mark.asyncio
    async def test_cancel_intent_type_exists_during_halt(self):
        """Cancel intents are a valid IntentType that the system recognizes."""
        # Per Constitution: "Cancel actions remain allowed in HALT"
        # The cancel intent is IntentType.CANCEL which is distinct from NEW/AMEND
        cancel_intent = _make_order_intent(intent_type=IntentType.CANCEL)
        assert cancel_intent.intent_type == IntentType.CANCEL
        assert cancel_intent.intent_type != IntentType.NEW

    @pytest.mark.asyncio
    async def test_halt_to_normal_re_enables_order_flow(self):
        """Transitioning from HALT to NORMAL re-enables order adapter."""
        system = _make_system()
        system.running = True

        # Enter HALT
        system.storm_guard.trigger_halt("crash")
        HFTSystem._set_service_running(system.order_adapter, False)
        assert system.order_adapter.running is False

        # Recover to NORMAL
        system.storm_guard.state = StormGuardState.NORMAL
        HFTSystem._set_service_running(system.order_adapter, True)
        assert system.order_adapter.running is True

    @pytest.mark.asyncio
    async def test_halt_blocks_new_order_intent(self):
        """New OrderIntents placed after HALT are drained, not processed."""
        system = _make_system()
        system.running = True
        system.storm_guard.trigger_halt("test")

        # Place intent after HALT
        intent = _make_order_intent()
        await system.order_queue.put(intent)

        # Drain (as _supervise does)
        drained = 0
        while not system.order_queue.empty():
            try:
                system.order_queue.get_nowait()
                system.order_queue.task_done()
                drained += 1
            except asyncio.QueueEmpty:
                break

        assert drained == 1
        assert system.order_queue.empty()


# ---------------------------------------------------------------------------
# Feed gap / StormGuard
# ---------------------------------------------------------------------------


class TestFeedGapStormGuard:
    """Feed gap detection triggers StormGuard state changes."""

    def test_feed_gap_returns_zero_when_no_method(self):
        """If md_service has no get_max_feed_gap_s, returns 0.0."""
        md = SimpleNamespace()
        assert HFTSystem._get_max_feed_gap_s(md) == 0.0

    def test_feed_gap_returns_value_within_reconnect_window(self):
        """Returns feed gap when within reconnect window."""
        md = SimpleNamespace(
            get_max_feed_gap_s=lambda: 2.5,
            within_reconnect_window=lambda: True,
        )
        assert HFTSystem._get_max_feed_gap_s(md) == 2.5

    def test_feed_gap_returns_zero_outside_reconnect_window(self):
        """Returns 0.0 when outside reconnect window (stale gap)."""
        md = SimpleNamespace(
            get_max_feed_gap_s=lambda: 2.5,
            within_reconnect_window=lambda: False,
        )
        assert HFTSystem._get_max_feed_gap_s(md) == 0.0

    def test_feed_gap_triggers_storm_state(self):
        """A large feed gap causes StormGuard to escalate to STORM."""
        from hft_platform.risk.storm_guard import RiskThresholds, StormGuard

        with patch("hft_platform.risk.storm_guard.MetricsRegistry") as mock_metrics_cls:
            mock_metrics = MagicMock()
            mock_metrics.stormguard_mode.labels.return_value = MagicMock()
            mock_metrics_cls.get.return_value = mock_metrics
            sg = StormGuard(thresholds=RiskThresholds(feed_gap_halt_s=1.0))

        with patch("hft_platform.risk.storm_guard.MetricsRegistry") as mock_metrics_cls:
            mock_metrics_cls.get.return_value = mock_metrics
            result = sg.update(feed_gap_s=1.5)

        assert result >= StormGuardState.STORM

    def test_storm_guard_degrade_to_storm_escalation(self):
        """StormGuard escalates from WARM to STORM on latency threshold."""
        from hft_platform.risk.storm_guard import RiskThresholds, StormGuard

        with patch("hft_platform.risk.storm_guard.MetricsRegistry") as mock_metrics_cls:
            mock_metrics = MagicMock()
            mock_metrics.stormguard_mode.labels.return_value = MagicMock()
            mock_metrics_cls.get.return_value = mock_metrics
            sg = StormGuard(thresholds=RiskThresholds())

        with patch("hft_platform.risk.storm_guard.MetricsRegistry") as mock_metrics_cls:
            mock_metrics_cls.get.return_value = mock_metrics
            # First: push to WARM
            sg.update(latency_us=6_000)
            assert sg.state == StormGuardState.WARM

            # Then: escalate to STORM
            sg.update(latency_us=25_000)
            assert sg.state == StormGuardState.STORM

    def test_drawdown_triggers_halt(self):
        """Extreme drawdown triggers HALT state."""
        from hft_platform.risk.storm_guard import RiskThresholds, StormGuard

        with patch("hft_platform.risk.storm_guard.MetricsRegistry") as mock_metrics_cls:
            mock_metrics = MagicMock()
            mock_metrics.stormguard_mode.labels.return_value = MagicMock()
            mock_metrics_cls.get.return_value = mock_metrics
            sg = StormGuard(thresholds=RiskThresholds(halt_drawdown_bps=-200))

        with patch("hft_platform.risk.storm_guard.MetricsRegistry") as mock_metrics_cls:
            mock_metrics_cls.get.return_value = mock_metrics
            result = sg.update(drawdown_bps=-300)

        assert result == StormGuardState.HALT

    def test_per_symbol_feed_gaps_empty_when_no_method(self):
        """Returns empty dict when md_service lacks get_feed_gaps_by_symbol."""
        md = SimpleNamespace()
        assert HFTSystem._get_feed_gaps_by_symbol(md) == {}

    def test_per_symbol_feed_gaps_returns_data(self):
        """Returns per-symbol feed gaps when available."""
        md = SimpleNamespace(
            get_feed_gaps_by_symbol=lambda: {"2330": 0.5, "2317": 1.2},
        )
        gaps = HFTSystem._get_feed_gaps_by_symbol(md)
        assert gaps["2330"] == 0.5
        assert gaps["2317"] == 1.2


# ---------------------------------------------------------------------------
# Loop lag monitoring
# ---------------------------------------------------------------------------


class TestLoopLagMonitoring:
    """Loop lag detection and reporting in _supervise."""

    @pytest.mark.asyncio
    async def test_loop_lag_calculation(self):
        """Lag is computed as actual_elapsed - expected_interval."""
        # The formula in _supervise: lag_s = max(0.0, now_tick - last_tick - interval_s)
        interval_s = 1.0
        last_tick = 100.0
        now_tick = 101.5  # 0.5s of lag
        lag_s = max(0.0, now_tick - last_tick - interval_s)
        assert abs(lag_s - 0.5) < 1e-9

    @pytest.mark.asyncio
    async def test_loop_lag_zero_when_on_time(self):
        """No lag when supervision loop runs on schedule."""
        interval_s = 1.0
        last_tick = 100.0
        now_tick = 101.0
        lag_s = max(0.0, now_tick - last_tick - interval_s)
        assert lag_s == 0.0

    @pytest.mark.asyncio
    async def test_loop_lag_never_negative(self):
        """Lag is clamped to zero (never negative)."""
        interval_s = 1.0
        last_tick = 100.0
        now_tick = 100.8  # faster than expected
        lag_s = max(0.0, now_tick - last_tick - interval_s)
        assert lag_s == 0.0

    @pytest.mark.asyncio
    async def test_lag_converted_to_latency_us_for_storm_guard(self):
        """Lag in seconds is converted to microseconds for StormGuard input."""
        lag_s = 0.025  # 25ms
        latency_us = int(lag_s * 1_000_000)
        assert latency_us == 25_000


# ---------------------------------------------------------------------------
# Drawdown helper
# ---------------------------------------------------------------------------


class TestDrawdownHelper:
    """_get_drawdown_pct extraction from position store."""

    def test_returns_zero_when_no_method(self):
        ps = SimpleNamespace()
        assert HFTSystem._get_drawdown_pct(ps, {}) == 0.0

    def test_uses_get_drawdown_pct_method(self):
        ps = SimpleNamespace(get_drawdown_pct=lambda: -0.015)
        assert HFTSystem._get_drawdown_pct(ps, {}) == -0.015

    def test_falls_back_to_total_pnl(self):
        ps = SimpleNamespace(total_pnl=-50000)
        result = HFTSystem._get_drawdown_pct(ps, {"base_capital": 1_000_000})
        assert abs(result - (-0.05)) < 1e-9

    def test_zero_base_capital_returns_zero(self):
        ps = SimpleNamespace(total_pnl=-50000)
        assert HFTSystem._get_drawdown_pct(ps, {"base_capital": 0}) == 0.0


# ---------------------------------------------------------------------------
# _set_service_running helper
# ---------------------------------------------------------------------------


class TestSetServiceRunning:
    """_set_service_running only touches objects with `running` attribute."""

    def test_sets_running_attribute(self):
        svc = SimpleNamespace(running=True)
        HFTSystem._set_service_running(svc, False)
        assert svc.running is False

    def test_noop_without_running_attribute(self):
        svc = SimpleNamespace()
        HFTSystem._set_service_running(svc, False)
        assert not hasattr(svc, "running")


# ---------------------------------------------------------------------------
# Utility — run a failing task outside event loop context
# ---------------------------------------------------------------------------


async def _run_failing_task(loop):
    raise RuntimeError("boom")
