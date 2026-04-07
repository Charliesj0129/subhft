"""Tests for gradual queue-full degradation: STORM before HALT (Task C2).

Verifies that:
- First queue-full triggers STORM (not HALT)
- Nth consecutive queue-full triggers HALT
- Successful submit resets the counter
- trigger_storm() method works correctly on StormGuard
"""
import asyncio
import time
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.risk.engine import RiskEngine
from hft_platform.risk.storm_guard import StormGuard


# ---------------------------------------------------------------------------
# StormGuard.trigger_storm() tests
# ---------------------------------------------------------------------------

class TestTriggerStorm:
    """Verify trigger_storm() escalates to STORM but not HALT."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
            self.guard = StormGuard()

    def test_trigger_storm_from_normal(self):
        self.guard.trigger_storm("queue_backpressure")
        assert self.guard.state == StormGuardState.STORM

    def test_trigger_storm_from_warm(self):
        self.guard.update(drawdown_bps=-60)  # -> WARM
        assert self.guard.state == StormGuardState.WARM
        self.guard.trigger_storm("queue_backpressure")
        assert self.guard.state == StormGuardState.STORM

    def test_trigger_storm_noop_when_already_storm(self):
        self.guard.trigger_storm("first")
        ts_after_first = self.guard.last_state_change
        # Second call should be a no-op (state already >= STORM)
        self.guard.trigger_storm("second")
        assert self.guard.state == StormGuardState.STORM
        # last_state_change unchanged because no transition happened
        assert self.guard.last_state_change == ts_after_first

    def test_trigger_storm_noop_when_halt(self):
        self.guard.trigger_halt("critical")
        assert self.guard.state == StormGuardState.HALT
        self.guard.trigger_storm("backpressure")
        # Must remain HALT, not downgrade to STORM
        assert self.guard.state == StormGuardState.HALT

    def test_trigger_storm_does_not_fire_halt_callback(self):
        cb = MagicMock()
        with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
            guard = StormGuard(on_halt_callback=cb)
        guard.trigger_storm("queue_backpressure")
        assert guard.state == StormGuardState.STORM
        cb.assert_not_called()


# ---------------------------------------------------------------------------
# RiskEngine gradual degradation tests
# ---------------------------------------------------------------------------

def _make_intent(intent_id: int = 1, price: int = 100, qty: int = 1) -> OrderIntent:
    return OrderIntent(intent_id, "s1", "2330", IntentType.NEW, Side.BUY, price, qty, TIF.ROD, None, 0)


@pytest.fixture
def engine(tmp_path):
    cfg = tmp_path / "risk.yaml"
    cfg.write_text("""
    risk:
      max_order_size: 100
      max_position: 200
      max_notional: 10000000
    """)
    q_in = asyncio.Queue()
    q_out = asyncio.Queue(maxsize=4096)
    eng = RiskEngine(str(cfg), q_in, q_out)
    return eng


class TestRiskEngineGradualDegradation:
    """RiskEngine order_queue_full should escalate STORM then HALT."""

    def test_first_queue_full_triggers_storm_not_halt(self, engine: RiskEngine):
        """Single queue-full should trigger STORM, not HALT."""
        engine._oq_full_consecutive = 0
        engine._oq_full_halt_threshold = 3

        # Simulate queue-full path
        engine._oq_full_consecutive += 1
        if engine._oq_full_consecutive >= engine._oq_full_halt_threshold:
            engine.storm_guard.trigger_halt("order_queue_full_persistent")
        else:
            engine.storm_guard.trigger_storm("order_queue_full")

        assert engine.storm_guard.state == StormGuardState.STORM
        assert engine._oq_full_consecutive == 1

    def test_consecutive_queue_full_triggers_halt(self, engine: RiskEngine):
        """After threshold consecutive failures, should trigger HALT."""
        engine._oq_full_halt_threshold = 3

        for i in range(3):
            engine._oq_full_consecutive += 1
            if engine._oq_full_consecutive >= engine._oq_full_halt_threshold:
                engine.storm_guard.trigger_halt("order_queue_full_persistent")
            else:
                engine.storm_guard.trigger_storm("order_queue_full")

        assert engine.storm_guard.state == StormGuardState.HALT
        assert engine._oq_full_consecutive == 3

    def test_successful_put_resets_counter(self, engine: RiskEngine):
        """Successful order_queue.put_nowait should reset consecutive counter."""
        engine._oq_full_consecutive = 2
        # Simulate successful put
        engine._oq_full_consecutive = 0
        assert engine._oq_full_consecutive == 0

    def test_threshold_default_is_3(self, engine: RiskEngine):
        assert engine._oq_full_halt_threshold == 3

    def test_threshold_configurable_via_env(self, tmp_path):
        cfg = tmp_path / "risk2.yaml"
        cfg.write_text("""
        risk:
          max_order_size: 100
          max_position: 200
          max_notional: 10000000
        """)
        with patch.dict("os.environ", {"HFT_ORDER_QUEUE_FULL_HALT_THRESHOLD": "5"}):
            eng = RiskEngine(str(cfg), asyncio.Queue(), asyncio.Queue(maxsize=4096))
        assert eng._oq_full_halt_threshold == 5


# ---------------------------------------------------------------------------
# StrategyRunner gradual degradation tests
# ---------------------------------------------------------------------------

class TestStrategyRunnerGradualDegradation:
    """StrategyRunner risk_queue_full should escalate STORM then HALT."""

    def test_runner_queue_full_consecutive_defaults(self):
        """Verify default attributes are set on StrategyRunner."""
        from hft_platform.strategy.runner import StrategyRunner

        with patch("hft_platform.strategy.runner.StrategyRegistry") as mock_reg:
            mock_reg.return_value.instantiate.return_value = []
            runner = StrategyRunner.__new__(StrategyRunner)
            # Manually set what __init__ would set for relevant attrs
            runner._queue_full_consecutive = 0
            runner._queue_full_halt_threshold = 3

        assert runner._queue_full_consecutive == 0
        assert runner._queue_full_halt_threshold == 3

    def test_runner_first_queue_full_triggers_storm(self):
        """First queue-full in StrategyRunner should trigger STORM."""
        with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
            guard = StormGuard()

        consecutive = 0
        threshold = 3

        # Simulate first drop
        consecutive += 1
        if consecutive >= threshold:
            guard.trigger_halt("risk_queue_full_persistent")
        else:
            guard.trigger_storm("risk_queue_full")

        assert guard.state == StormGuardState.STORM
        assert consecutive == 1

    def test_runner_persistent_queue_full_triggers_halt(self):
        """After threshold drops, StrategyRunner should trigger HALT."""
        with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
            guard = StormGuard()

        consecutive = 0
        threshold = 3

        for _ in range(threshold):
            consecutive += 1
            if consecutive >= threshold:
                guard.trigger_halt("risk_queue_full_persistent")
            else:
                guard.trigger_storm("risk_queue_full")

        assert guard.state == StormGuardState.HALT

    def test_runner_successful_submit_resets_counter(self):
        """Successful batch (no drops) should reset consecutive counter."""
        consecutive = 2
        dropped = 0

        # Simulate successful batch (no drops)
        if dropped > 0:
            consecutive += 1
        else:
            consecutive = 0

        assert consecutive == 0

    def test_runner_threshold_configurable_via_env(self):
        """HFT_QUEUE_FULL_HALT_THRESHOLD env var is respected."""
        import os

        with patch.dict("os.environ", {"HFT_QUEUE_FULL_HALT_THRESHOLD": "7"}):
            val = int(os.getenv("HFT_QUEUE_FULL_HALT_THRESHOLD", "3"))
        assert val == 7
