"""D3: Timeout/cooldown paths must use monotonic clock, not wall-clock."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestStormGuardMonotonic:
    """Sites 3a-3c: StormGuard cooldown uses time.monotonic()."""

    def test_storm_entry_ts_uses_monotonic(self):
        from hft_platform.risk.storm_guard import StormGuard, StormGuardState

        sg = StormGuard()
        sg.metrics = MagicMock()

        with patch("time.monotonic", return_value=1000.0):
            sg.state = StormGuardState.NORMAL
            sg._de_escalate_count = 0
            sg.update(drawdown_bps=sg.thresholds.storm_drawdown_bps - 1)

        assert sg._storm_entry_ts == 1000.0, f"Expected monotonic value 1000.0, got {sg._storm_entry_ts}"

    def test_halt_cooldown_uses_monotonic(self):
        from hft_platform.risk.storm_guard import StormGuard, StormGuardState

        sg = StormGuard()
        sg.metrics = MagicMock()
        sg._halt_cooldown_s = 10.0
        sg._de_escalate_threshold = 1

        with patch("time.monotonic", return_value=100.0):
            sg.trigger_halt("test")

        assert sg._halt_entry_ts == 100.0

        with patch("time.monotonic", return_value=105.0):
            sg.update(drawdown_bps=0)
        assert sg.state == StormGuardState.HALT

        with patch("time.monotonic", return_value=111.0):
            sg.update(drawdown_bps=0)
        assert sg.state != StormGuardState.HALT

    def test_last_state_change_uses_monotonic(self):
        from hft_platform.risk.storm_guard import StormGuard, StormGuardState

        sg = StormGuard()
        sg.metrics = MagicMock()

        with patch("time.monotonic", return_value=42.0):
            sg.update(drawdown_bps=sg.thresholds.warm_drawdown_bps - 1)

        assert sg.last_state_change == 42.0


class TestCircuitBreakerMonotonic:
    """Site 3d: OrderAdapter circuit breaker uses time.monotonic()."""

    def test_circuit_breaker_open_until_uses_monotonic(self):
        from hft_platform.order.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(threshold=2, timeout_s=30)

        with patch("time.monotonic", return_value=500.0):
            cb.record_failure()
            cb.record_failure()

        assert cb.open_until == 530.0

    def test_circuit_breaker_is_open_uses_monotonic(self):
        from hft_platform.order.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(threshold=1, timeout_s=10)

        with patch("time.monotonic", return_value=100.0):
            cb.record_failure()

        with patch("time.monotonic", return_value=105.0):
            assert cb.is_open() is True

        with patch("time.monotonic", return_value=111.0):
            assert cb.is_open() is False


class TestRiskEngineDeadlineMonotonic:
    """Site 3g: RiskEngine deadline_ns uses time.monotonic_ns()."""

    def test_deadline_uses_monotonic_ns(self):
        import asyncio

        from hft_platform.risk.engine import RiskEngine

        q_in = asyncio.Queue()
        q_out = asyncio.Queue()
        engine = RiskEngine.__new__(RiskEngine)
        engine.intent_queue = q_in
        engine.order_queue = q_out
        engine.metrics = MagicMock()
        engine.storm_guard = MagicMock()
        engine.storm_guard.state = 0
        engine._cmd_counter = 0
        engine.latency = None
        engine._reject_metric_cache = {}
        engine._reject_metric_cache_owner_id = None
        engine._reject_metric_counter = 0
        engine._trace_sampler = None
        engine._cmd_id_lock_enabled = False
        engine._cmd_id_lock = None
        engine._monotonic_cmd_id = 0

        mock_intent = MagicMock()
        mock_intent.trace_id = "t1"

        with patch("time.monotonic_ns", return_value=5_000_000_000):
            cmd = engine.create_command(mock_intent)

        assert cmd.deadline_ns == 5_500_000_000, f"Expected 5_500_000_000, got {cmd.deadline_ns}"
