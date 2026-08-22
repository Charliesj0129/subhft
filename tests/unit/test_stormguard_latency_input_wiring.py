"""Both StormGuard latency inputs were connected to the wrong thing.

Measured on THESHOW over the 22 h following the 2026-08-21 15:45Z restart:

* ``stormguard_latency_input_max_us{input="order_rtt"}`` was **0**, and
  ``order_rtt`` appeared nowhere in ``src/`` outside ``storm_guard.py``. The
  input had no producer at all -- a structural zero, which reads exactly like a
  healthy input rather than a dead one. But the measurement was never missing:
  ``OrderAdapter`` has always timed the broker SDK call and published it as
  ``pipeline_latency_ns{stage="api_place_order"}`` (n=7, mean 34.1 ms, 6 samples
  in (10, 50] ms and 1 in (50, 100] ms, unsampled -- ``HFT_OBS_POLICY`` unset).
  It was a wiring gap, not a measurement gap.

* ``loop_lag`` was armed at 5 s WARM / 10 s STORM and fed ``event_loop_lag_ms``,
  which spans the supervisor's whole tick period. Splitting it with the two
  metrics that already exist shows the supervisor's own body is **89.3%** of it
  (``supervisor_tick_duration_ms`` mean 4.93 ms vs ``event_loop_probe_lag_ms``
  mean 0.59 ms). The breaker was armed on a number that mostly measured the
  supervisor timing itself. Real congestion: 99.895% of probe samples <= 10 ms,
  exactly one in (500, 1000] ms, nothing above 1 s.

Neither input contributed to a single state change in that window.

The order-RTT input still ships **unarmed**. Arming a risk breaker is a
calibration decision and n=7 is a sample, not a distribution; what changes here
is that the input now carries real values for that decision to be made from.
"""

from __future__ import annotations

from typing import Any

import pytest

from hft_platform.contracts.strategy import StormGuardState
from hft_platform.risk.storm_guard import StormGuard


@pytest.fixture(autouse=True)
def _reset_input_gauges():
    from hft_platform.observability.metrics import MetricsRegistry

    metrics = MetricsRegistry.get()
    for name in ("loop_lag", "order_rtt"):
        metrics.stormguard_latency_input_max_us.labels(input=name).set(0)
    yield


def _guard(**thresholds: object) -> StormGuard:
    guard = StormGuard()
    for key, value in thresholds.items():
        setattr(guard.thresholds, key, value)
    return guard


class TestTheOrderRttInputNowHasAProducer:
    def test_a_recorded_round_trip_reaches_the_next_update(self) -> None:
        guard = _guard(order_rtt_storm_us=200_000)
        guard.observe_order_rtt_us(250_000)
        assert guard.update() is StormGuardState.STORM

    def test_the_largest_round_trip_in_the_window_is_the_one_evaluated(self) -> None:
        guard = _guard(order_rtt_storm_us=200_000)
        guard.observe_order_rtt_us(10_000)
        guard.observe_order_rtt_us(250_000)
        guard.observe_order_rtt_us(20_000)
        assert guard.update() is StormGuardState.STORM

    def test_an_explicit_argument_still_wins_when_it_is_larger(self) -> None:
        guard = _guard(order_rtt_storm_us=200_000)
        guard.observe_order_rtt_us(1_000)
        assert guard.update(order_rtt_us=250_000) is StormGuardState.STORM

    def test_the_observed_maximum_gauge_now_moves(self) -> None:
        """A zero here used to mean "no producer"; it must now mean "no traffic"."""
        from hft_platform.observability.metrics import MetricsRegistry

        gauge = MetricsRegistry.get().stormguard_latency_input_max_us.labels(input="order_rtt")
        guard = _guard()
        guard.observe_order_rtt_us(34_146)
        guard.update()
        assert gauge._value.get() == 34_146

    def test_a_non_positive_round_trip_is_ignored(self) -> None:
        guard = _guard(order_rtt_storm_us=200_000)
        guard.observe_order_rtt_us(0)
        guard.observe_order_rtt_us(-5)
        assert guard.update() is StormGuardState.NORMAL


class TestObservingIsNotEscalating:
    """PR #440's lesson: an escalation ``update()`` cannot see is not a latch."""

    def test_observing_alone_does_not_change_state(self) -> None:
        guard = _guard(order_rtt_storm_us=200_000)
        guard.observe_order_rtt_us(10_000_000)
        assert guard.state is StormGuardState.NORMAL

    def test_one_spike_is_evaluated_once_and_not_held_across_updates(self) -> None:
        guard = _guard(order_rtt_storm_us=200_000)
        guard.observe_order_rtt_us(250_000)
        guard.update()
        assert guard._drain_order_rtt_peak_us() == 0

    def test_a_quiet_window_reports_zero_not_the_previous_peak(self) -> None:
        guard = _guard()
        guard.observe_order_rtt_us(250_000)
        assert guard._drain_order_rtt_peak_us() == 250_000
        assert guard._drain_order_rtt_peak_us() == 0


class TestTheAdapterReportsWhatItAlreadyMeasures:
    """The adapter is where the broker round-trip is timed; it must report it."""

    @staticmethod
    def _adapter(guard: Any) -> Any:
        from hft_platform.order.adapter import OrderAdapter

        adapter = OrderAdapter.__new__(OrderAdapter)
        adapter._storm_guard = guard
        return adapter

    class _Recorder:
        def __init__(self) -> None:
            self.seen: list[int] = []

        def observe_order_rtt_us(self, rtt_us: int) -> None:
            self.seen.append(rtt_us)

    def test_a_place_order_round_trip_is_reported_in_microseconds(self) -> None:
        recorder = self._Recorder()
        self._adapter(recorder)._observe_order_rtt("place_order", 34_146_000)
        assert recorder.seen == [34_146]

    @pytest.mark.parametrize("op", ["place_order", "cancel_order", "update_order"])
    def test_every_broker_order_op_is_reported(self, op: str) -> None:
        recorder = self._Recorder()
        self._adapter(recorder)._observe_order_rtt(op, 1_000_000)
        assert recorder.seen == [1_000]

    def test_a_non_order_op_is_not_reported(self) -> None:
        recorder = self._Recorder()
        self._adapter(recorder)._observe_order_rtt("list_positions", 1_000_000)
        assert recorder.seen == []

    def test_no_storm_guard_bound_is_not_an_error(self) -> None:
        """Unit-test construction leaves it None; an order must not fail for it.

        Asserted by binding a guard afterwards: the unbound call must leave the
        adapter able to report the next round-trip, not quietly wedged.
        """
        adapter = self._adapter(None)
        adapter._observe_order_rtt("place_order", 1_000_000)
        recorder = self._Recorder()
        adapter._storm_guard = recorder
        adapter._observe_order_rtt("place_order", 1_000_000)
        assert recorder.seen == [1_000]

    def test_a_raising_storm_guard_never_fails_an_order(self) -> None:
        """The swallow must cover a real call -- not an op that was filtered out."""

        class Exploding:
            def __init__(self) -> None:
                self.calls = 0

            def observe_order_rtt_us(self, rtt_us: int) -> None:
                self.calls += 1
                raise RuntimeError("boom")

        exploding = Exploding()
        self._adapter(exploding)._observe_order_rtt("place_order", 1_000_000)
        assert exploding.calls == 1, "the exception path was never reached"


class TestTheLoopLagInputReadsTheIdleProbe:
    @staticmethod
    def _system() -> Any:
        from hft_platform.services.system import HFTSystem

        return HFTSystem.__new__(HFTSystem)

    def test_the_probe_peak_is_used_when_the_probe_has_samples(self) -> None:
        system = self._system()
        system._loop_probe_samples = 42
        system._loop_probe_peak_ms = 12.5
        # lag_s would have reported 5 s; the probe says 12.5 ms of real congestion.
        assert system._stormguard_latency_us(5.0) == 12_500

    def test_the_peak_is_drained_so_a_spike_is_evaluated_once(self) -> None:
        system = self._system()
        system._loop_probe_samples = 42
        system._loop_probe_peak_ms = 900.0
        assert system._stormguard_latency_us(0.0) == 900_000
        system._loop_probe_samples = 42  # the probe is still running
        assert system._stormguard_latency_us(0.0) == 0

    def test_a_probe_that_stopped_reporting_falls_back_instead_of_reading_zero(self) -> None:
        """The probe is started unsupervised, so it can die without HALTing anything.

        A cumulative sample count would keep selecting the probe branch forever
        after its first sample, and a dead probe's peak is 0.0 -- which a
        latency breaker reads as a perfectly healthy loop.
        """
        system = self._system()
        system._loop_probe_samples = 42
        system._loop_probe_peak_ms = 3.0
        assert system._stormguard_latency_us(5.0) == 3_000
        # No new samples arrived: the probe is gone. Fall back to the
        # supervisor's own signal rather than reporting a healthy loop.
        assert system._stormguard_latency_us(5.0) == 5_000_000

    def test_a_clean_loop_reports_zero_rather_than_the_supervisors_own_work(self) -> None:
        system = self._system()
        system._loop_probe_samples = 42
        system._loop_probe_peak_ms = 0.0
        # 5.36 ms was THESHOW's median composite; 89.3% of it was the supervisor.
        assert system._stormguard_latency_us(0.00536) == 0

    def test_it_falls_back_while_the_probe_has_produced_nothing(self) -> None:
        """A breaker must not be handed a confident zero by an input that is not running."""
        system = self._system()
        assert system._loop_probe_samples == 0
        assert system._stormguard_latency_us(5.0) == 5_000_000
