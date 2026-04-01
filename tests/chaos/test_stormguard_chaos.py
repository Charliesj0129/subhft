"""Chaos tests for StormGuard risk state machine.

Tests concurrent access, callback isolation, rapid state transitions,
and edge cases under load.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.risk.storm_guard import RiskThresholds, StormGuard, StormGuardState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MOCK_TIME = 1_000_000.0


@pytest.fixture(autouse=True)
def _patch_externals():
    """Patch MetricsRegistry, audit writer, and timebase for all chaos tests."""
    mock_metrics = MagicMock()
    mock_audit = MagicMock()
    time_val = [_MOCK_TIME]

    def _now_s():
        return time_val[0]

    def _advance(s: float):
        time_val[0] += s

    with (
        patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=mock_metrics),
        patch("hft_platform.recorder.audit.get_audit_writer", return_value=mock_audit),
        patch("hft_platform.risk.storm_guard.timebase") as mock_tb,
    ):
        mock_tb.now_s = _now_s
        mock_tb.now_ns = lambda: int(time_val[0] * 1e9)
        # Expose helpers via the fixture indirectly through closure
        _patch_externals.advance = _advance  # type: ignore[attr-defined]
        _patch_externals.time_val = time_val  # type: ignore[attr-defined]
        _patch_externals.mock_metrics = mock_metrics  # type: ignore[attr-defined]
        yield


def _advance_time(seconds: float) -> None:
    _patch_externals.advance(seconds)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.chaos
class TestStormGuardChaos:
    """Chaos engineering tests for StormGuard."""

    # 1. Concurrent HALT propagation -----------------------------------------
    def test_concurrent_halt_propagation(self):
        """10 threads trigger_halt concurrently; final state must be HALT."""
        guard = StormGuard()
        barrier = threading.Barrier(10)
        errors: list[Exception] = []

        def _halt(idx: int):
            try:
                barrier.wait(timeout=5)
                guard.trigger_halt(f"thread-{idx}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_halt, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"
        assert guard.state == StormGuardState.HALT
        assert not guard.is_safe()

    # 2. HALT blocks order flow under load -----------------------------------
    def test_halt_blocks_order_flow_under_load(self):
        """50 threads verify is_safe() returns False after HALT."""
        guard = StormGuard()
        guard.trigger_halt("chaos-test")
        barrier = threading.Barrier(50)
        results: list[bool] = []
        lock = threading.Lock()

        def _check():
            barrier.wait(timeout=5)
            safe = guard.is_safe()
            with lock:
                results.append(safe)

        threads = [threading.Thread(target=_check) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(results) == 50
        assert all(r is False for r in results)

    # 3. Callback exception isolation ----------------------------------------
    def test_callback_exception_isolation(self):
        """on_halt_callback raises; guard must still reach HALT."""

        def _bad_callback():
            raise RuntimeError("callback boom")

        guard = StormGuard(on_halt_callback=_bad_callback)
        guard.trigger_halt("chaos-exc")
        assert guard.state == StormGuardState.HALT

    # 4. Rapid state oscillation ---------------------------------------------
    def test_rapid_state_oscillation(self):
        """Oscillate NORMAL/WARM 1000 times without crash."""
        guard = StormGuard(thresholds=RiskThresholds(warm_drawdown_bps=-50))
        for _ in range(1000):
            guard.update(drawdown_bps=-60)  # -> WARM
            # WARM -> NORMAL requires de-escalation threshold; force via transition
            guard._transition(StormGuardState.NORMAL, "reset")
        # Should be NORMAL after last manual transition
        assert guard.state == StormGuardState.NORMAL

    # 5. Recovery under concurrent load --------------------------------------
    def test_recovery_under_concurrent_load(self):
        """HALT->NORMAL while concurrent readers check is_safe()."""
        guard = StormGuard()
        guard._halt_cooldown_s = 0.0
        guard._de_escalate_threshold = 1
        guard.trigger_halt("pre-recovery")
        go_event = threading.Event()
        saw_safe: list[bool] = []
        lock = threading.Lock()

        def _reader():
            go_event.wait(timeout=5)
            for _ in range(100):
                safe = guard.is_safe()
                with lock:
                    saw_safe.append(safe)

        threads = [threading.Thread(target=_reader) for _ in range(10)]
        for t in threads:
            t.start()
        # Trigger recovery then signal readers
        guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
        go_event.set()
        for t in threads:
            t.join(timeout=10)

        # After recovery, guard should be NORMAL (safe=True)
        assert guard.state == StormGuardState.NORMAL
        # At least some readers should see safe=True
        assert any(r is True for r in saw_safe)

    # 6. Concurrent state reads with single writer ---------------------------
    def test_concurrent_state_reads(self):
        """10 readers + 1 writer; readers always see a valid StormGuardState."""
        guard = StormGuard()
        stop = threading.Event()
        invalid_states: list[int] = []
        lock = threading.Lock()
        valid_values = {int(s) for s in StormGuardState}

        def _reader():
            while not stop.is_set():
                val = int(guard.state)
                if val not in valid_values:
                    with lock:
                        invalid_states.append(val)

        def _writer():
            for i in range(200):
                if i % 3 == 0:
                    guard.trigger_halt(f"w-{i}")
                elif i % 3 == 1:
                    guard._transition(StormGuardState.WARM, f"w-{i}")
                else:
                    guard._transition(StormGuardState.NORMAL, f"w-{i}")

        readers = [threading.Thread(target=_reader) for _ in range(10)]
        writer = threading.Thread(target=_writer)
        for t in readers:
            t.start()
        writer.start()
        writer.join(timeout=10)
        stop.set()
        for t in readers:
            t.join(timeout=10)

        assert not invalid_states, f"Invalid states observed: {invalid_states}"

    # 7. HALT callback invoked at least once per transition ------------------
    def test_halt_callback_called_per_transition(self):
        """Callback fires on each HALT transition."""
        call_count = 0
        lock = threading.Lock()

        def _cb():
            nonlocal call_count
            with lock:
                call_count += 1

        guard = StormGuard(on_halt_callback=_cb)
        for _ in range(5):
            guard.trigger_halt("cycle")
            guard._transition(StormGuardState.NORMAL, "reset")

        assert call_count >= 5

    # 8. De-escalation under concurrent load ---------------------------------
    def test_deescalation_under_load(self):
        """Concurrent clear evaluations eventually de-escalate from STORM."""
        guard = StormGuard(thresholds=RiskThresholds(storm_drawdown_bps=-100))
        guard._storm_cooldown_s = 0.0  # disable cooldown for test speed
        guard._de_escalate_threshold = 3
        # Escalate to STORM
        guard.update(drawdown_bps=-150)
        assert guard.state == StormGuardState.STORM

        _advance_time(1.0)  # ensure cooldown passes

        barrier = threading.Barrier(5)
        results: list[StormGuardState] = []
        lock = threading.Lock()

        def _clear_eval():
            barrier.wait(timeout=5)
            for _ in range(10):
                st = guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
                with lock:
                    results.append(st)

        threads = [threading.Thread(target=_clear_eval) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # After many clear evaluations, should de-escalate
        assert guard.state.value <= StormGuardState.WARM.value

    # 9. Threshold update during active checking -----------------------------
    def test_threshold_update_during_active_checking(self):
        """Reload thresholds while update() is being called."""
        guard = StormGuard()
        stop = threading.Event()

        def _updater():
            while not stop.is_set():
                guard.update(drawdown_bps=-60, latency_us=100)

        t = threading.Thread(target=_updater)
        t.start()
        # Change thresholds mid-flight
        guard.reload_thresholds({"risk": {"warm_drawdown_bps": -10, "halt_drawdown_bps": -50}})
        time.sleep(0.05)
        stop.set()
        t.join(timeout=10)

        # -60 bps is now >= halt threshold of -50 -> should be HALT
        guard.update(drawdown_bps=-60)
        assert guard.state == StormGuardState.HALT

    # 10. Storm cooldown timing (mock time) ----------------------------------
    def test_storm_cooldown_timing(self):
        """STORM does not de-escalate before cooldown period elapses."""
        guard = StormGuard(thresholds=RiskThresholds(storm_drawdown_bps=-100))
        guard._storm_cooldown_s = 30.0
        guard._de_escalate_threshold = 1

        guard.update(drawdown_bps=-150)
        assert guard.state == StormGuardState.STORM

        # Before cooldown: clear eval should NOT de-escalate
        for _ in range(10):
            guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
        assert guard.state == StormGuardState.STORM

        # Advance time past cooldown
        _advance_time(31.0)
        guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
        assert guard.state == StormGuardState.NORMAL

    # 11. Feed gap triggers STORM (not HALT) ---------------------------------
    def test_feed_gap_storm_trigger(self):
        """Feed gap >= threshold triggers STORM (architecture: feed gap does not HALT)."""
        guard = StormGuard(thresholds=RiskThresholds(feed_gap_storm_s=1.0))
        state = guard.update(feed_gap_s=1.5)
        assert state == StormGuardState.STORM

    # 12. Drawdown cascade WARM -> STORM -> HALT -----------------------------
    def test_drawdown_cascade(self):
        """Increasing drawdown escalates through all states."""
        guard = StormGuard(
            thresholds=RiskThresholds(
                warm_drawdown_bps=-50,
                storm_drawdown_bps=-100,
                halt_drawdown_bps=-200,
            )
        )
        s1 = guard.update(drawdown_bps=-60)
        assert s1 == StormGuardState.WARM

        s2 = guard.update(drawdown_bps=-150)
        assert s2 == StormGuardState.STORM

        s3 = guard.update(drawdown_bps=-250)
        assert s3 == StormGuardState.HALT

    # 13. Concurrent drawdown updates ----------------------------------------
    def test_concurrent_drawdown_updates(self):
        """Multiple threads update drawdown; state must be a valid StormGuardState."""
        guard = StormGuard()
        barrier = threading.Barrier(10)
        final_states: list[StormGuardState] = []
        lock = threading.Lock()

        def _update(dd: int):
            barrier.wait(timeout=5)
            for _ in range(50):
                s = guard.update(drawdown_bps=dd)
                with lock:
                    final_states.append(s)

        threads = [
            threading.Thread(target=_update, args=(dd,)) for dd in [-10, -60, -110, -210, 0, -60, -110, -210, 0, -10]
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # All observed states must be valid
        valid = set(StormGuardState)
        assert all(s in valid for s in final_states)

    # 14. State metric consistency -------------------------------------------
    def test_state_metric_consistency(self):
        """Metric set() value matches guard.state after each transition."""
        mock_metrics = _patch_externals.mock_metrics  # type: ignore[attr-defined]
        guard = StormGuard()

        guard.trigger_halt("metric-test")
        mock_metrics.stormguard_mode.labels.return_value.set.assert_called_with(int(StormGuardState.HALT))

        guard._transition(StormGuardState.NORMAL, "reset")
        mock_metrics.stormguard_mode.labels.return_value.set.assert_called_with(int(StormGuardState.NORMAL))

    # 15. HALT -> NORMAL recovery cycle --------------------------------------
    def test_halt_normal_recovery_cycle(self):
        """Full HALT -> NORMAL -> HALT -> NORMAL cycle; verify state correctness."""
        guard = StormGuard()
        guard._halt_cooldown_s = 0.0
        guard._de_escalate_threshold = 1
        observed_halt = False
        observed_normal_after_halt = False

        for _ in range(10):
            guard.trigger_halt("cycle")
            assert guard.state == StormGuardState.HALT
            assert not guard.is_safe()
            observed_halt = True

            guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
            assert guard.state == StormGuardState.NORMAL
            assert guard.is_safe()
            observed_normal_after_halt = True

        assert observed_halt
        assert observed_normal_after_halt
        assert guard.state == StormGuardState.NORMAL
