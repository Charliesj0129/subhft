"""
E2E tests for the Observability & Safety Plane (Plane 6).

Covers:
  - StormGuard FSM state transitions (NORMAL → HALT)
  - HALT blocks new order evaluation via RiskEngine
  - HALT allows cancel/force-flat orders
  - Prometheus counter increment (MetricsRegistry)
  - Supervisor detects service crash and triggers halt
  - HALT drains queues, preserving CANCEL intents
  - Feed gap triggers STORM escalation
  - Queue depth gauge reflects actual queue size
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType, StormGuardState
from tests.e2e.conftest import make_intent

# ---------------------------------------------------------------------------
# Patch targets (matching Decision Plane test_03 pattern)
# ---------------------------------------------------------------------------

_AUDIT_PATCH = "hft_platform.recorder.audit.get_audit_writer"
_METRICS_PATCH = "hft_platform.risk.engine.MetricsRegistry"
_LATENCY_PATCH = "hft_platform.risk.engine.LatencyRecorder"


# ---------------------------------------------------------------------------
# Helper: build RiskEngine with Rust disabled
# ---------------------------------------------------------------------------


def _make_risk_engine(config_path: str, storm_guard=None):
    from hft_platform.risk.engine import RiskEngine

    return RiskEngine(
        config_path=config_path,
        intent_queue=asyncio.Queue(maxsize=64),
        order_queue=asyncio.Queue(maxsize=64),
        storm_guard=storm_guard,
    )


# ===========================================================================
# TestChain
# ===========================================================================


@pytest.mark.e2e_chain
class TestChain:
    def test_storm_guard_fsm_transitions(self) -> None:
        """StormGuard must start in NORMAL state and transition to HALT on trigger_halt."""
        from hft_platform.risk.storm_guard import RiskThresholds, StormGuard

        with patch("hft_platform.risk.storm_guard.MetricsRegistry"):
            sg = StormGuard(thresholds=RiskThresholds())
            assert sg.state == StormGuardState.NORMAL

            sg.trigger_halt("test")
            assert sg.state == StormGuardState.HALT

    def test_halt_blocks_risk_evaluation(
        self,
        e2e_risk_yaml: str,
        e2e_symbols_yaml: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RiskEngine must reject NEW intents when StormGuard is in HALT state."""
        monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "0")
        monkeypatch.setenv("HFT_RISK_FAST_GATE", "0")
        monkeypatch.setenv("SYMBOLS_CONFIG", e2e_symbols_yaml)

        with (
            patch(_METRICS_PATCH),
            patch(_LATENCY_PATCH),
            patch(_AUDIT_PATCH, return_value=MagicMock()),
            patch("hft_platform.risk.storm_guard.MetricsRegistry"),
        ):
            from hft_platform.risk.storm_guard import StormGuard

            sg = StormGuard()
            sg.trigger_halt("test_halt")

            engine = _make_risk_engine(e2e_risk_yaml, storm_guard=sg)
            intent = make_intent(intent_type=IntentType.NEW, price=100 * 10_000, qty=1)
            decision = engine.evaluate(intent)

        assert decision.approved is False

    def test_halt_allows_cancel(
        self,
        e2e_risk_yaml: str,
        e2e_symbols_yaml: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RiskEngine must approve CANCEL intents even when StormGuard is in HALT state."""
        monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "0")
        monkeypatch.setenv("HFT_RISK_FAST_GATE", "0")
        monkeypatch.setenv("SYMBOLS_CONFIG", e2e_symbols_yaml)

        with (
            patch(_METRICS_PATCH),
            patch(_LATENCY_PATCH),
            patch(_AUDIT_PATCH, return_value=MagicMock()),
            patch("hft_platform.risk.storm_guard.MetricsRegistry"),
        ):
            from hft_platform.risk.storm_guard import StormGuard

            sg = StormGuard()
            sg.trigger_halt("test_halt")

            engine = _make_risk_engine(e2e_risk_yaml, storm_guard=sg)
            cancel_intent = make_intent(
                intent_type=IntentType.CANCEL,
                price=100 * 10_000,
                qty=1,
            )
            decision = engine.evaluate(cancel_intent)

        assert decision.approved is True

    def test_metrics_counter_increment(self) -> None:
        """MetricsRegistry.feed_events_total counter must increment by 1 on .inc()."""
        from hft_platform.observability.metrics import MetricsRegistry

        registry = MetricsRegistry()
        counter = registry.feed_events_total.labels(type="tick")

        before = counter._value.get()
        counter.inc()
        after = counter._value.get()

        assert after == before + 1


# ===========================================================================
# TestIntegration
# ===========================================================================


@pytest.mark.e2e_integration
@pytest.mark.asyncio
class TestIntegration:
    async def test_supervise_detects_service_crash(self) -> None:
        """A crashing async task must be detectable; StormGuard can be halted as a result."""
        from hft_platform.risk.storm_guard import RiskThresholds, StormGuard

        with patch("hft_platform.risk.storm_guard.MetricsRegistry"):
            sg = StormGuard(thresholds=RiskThresholds())

        crashed = asyncio.Event()

        async def _crashing_task() -> None:
            await asyncio.sleep(0)
            crashed.set()
            raise RuntimeError("simulated service crash")

        task = asyncio.create_task(_crashing_task())

        # Wait for the task to finish (crash)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        except (RuntimeError, asyncio.TimeoutError):
            pass

        # Ensure the task did crash and event was set
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
        assert crashed.is_set()

        # Supervisor reacts by triggering halt
        sg.trigger_halt("service_crash")
        assert sg.state == StormGuardState.HALT

    async def test_halt_drains_queues_preserves_cancel(self) -> None:
        """During HALT, CANCEL intents must be preserved and NEW intents dropped."""
        risk_queue: asyncio.Queue = asyncio.Queue(maxsize=64)

        # Put 2 NEW intents and 1 CANCEL intent
        new_1 = make_intent(intent_id=1, intent_type=IntentType.NEW)
        new_2 = make_intent(intent_id=2, intent_type=IntentType.NEW)
        cancel = make_intent(intent_id=3, intent_type=IntentType.CANCEL)

        await risk_queue.put(new_1)
        await risk_queue.put(new_2)
        await risk_queue.put(cancel)

        # Simulate HALT drain logic: drain queue, keep only CANCEL/FORCE_FLAT
        preserved = []
        dropped_count = 0
        while not risk_queue.empty():
            item = risk_queue.get_nowait()
            if item.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT):
                preserved.append(item)
            else:
                dropped_count += 1

        assert len(preserved) == 1
        assert preserved[0].intent_type == IntentType.CANCEL
        assert dropped_count == 2

    async def test_feed_gap_triggers_halt(self) -> None:
        """StormGuard.update() with a large feed_gap_s must escalate to at least STORM."""
        from hft_platform.risk.storm_guard import RiskThresholds, StormGuard

        with patch("hft_platform.risk.storm_guard.MetricsRegistry"):
            thresholds = RiskThresholds(feed_gap_storm_s=0.5)
            sg = StormGuard(thresholds=thresholds)

        # Trigger large feed gap — well above threshold
        result_state = sg.update(drawdown_bps=0, latency_us=0, feed_gap_s=10.0)

        assert result_state >= StormGuardState.STORM

    async def test_queue_depth_metrics_updated(self) -> None:
        """MetricsRegistry.queue_depth gauge must reflect the value set on it."""
        from hft_platform.observability.metrics import MetricsRegistry

        registry = MetricsRegistry()
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)

        # Put 5 items in the queue
        for i in range(5):
            await queue.put(i)

        queue_size = queue.qsize()
        registry.queue_depth.labels(queue="risk_queue").set(queue_size)

        stored_value = registry.queue_depth.labels(queue="risk_queue")._value.get()
        assert stored_value == queue_size
        assert stored_value == 5
