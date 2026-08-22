"""The latency breaker measures the event loop and is calibrated for the broker.

Two different quantities were being funnelled through one parameter: platform
health (loop congestion, sub-millisecond, budget 1 ms) and trading-path health
(order round-trip, tens to hundreds of milliseconds). One threshold cannot
serve both. This module splits them, ships the order-RTT input **unarmed**, and
publishes each input's observed maximum plus an explicit armed/unarmed gauge so
an unarmed breaker cannot be mistaken for a quiet one.

Two claims in the original version of this docstring were later measured and
found wrong; see ``test_stormguard_latency_input_wiring.py`` for the evidence.

* It said ``system.py`` computes ``latency_us = int(lag_s * 1_000_000)`` and
  called that "event-loop lag, and nothing else". That value spans the
  supervisor's whole tick period: on THESHOW the supervisor's own body was
  89.3% of it. The supervisor now feeds the idle probe instead.
* It said "THESHOW has no order-RTT samples at all". It had them all along, as
  ``pipeline_latency_ns{stage="api_place_order"}`` (mean 34.1 ms) -- what was
  missing was the wiring from the adapter into the breaker, now in place.

The order-RTT input still ships unarmed: arming a risk breaker is a calibration
decision, and it is now made against real values rather than a structural zero.
"""

from __future__ import annotations

import pytest

from hft_platform.contracts.strategy import StormGuardState
from hft_platform.risk.storm_guard import RiskThresholds, StormGuard


@pytest.fixture(autouse=True)
def _reset_input_gauges():
    """The observed maximum is process-scoped by design — it answers "has this
    input ever come near its threshold since boot?". That makes it survive
    across tests in one interpreter, so each test starts from a known zero.
    """
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


# --------------------------------------------------------------------------- #
# Two inputs, two thresholds                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_an_order_round_trip_can_escalate_on_its_own_threshold() -> None:
    """The trading-path input, at a scale where loop lag never goes."""
    guard = _guard(order_rtt_warm_us=200_000, order_rtt_storm_us=500_000)

    assert guard.update(order_rtt_us=600_000) is StormGuardState.STORM


@pytest.mark.unit
def test_an_order_round_trip_warns_below_the_storm_threshold() -> None:
    guard = _guard(order_rtt_warm_us=200_000, order_rtt_storm_us=500_000)

    assert guard.update(order_rtt_us=250_000) is StormGuardState.WARM


@pytest.mark.unit
def test_loop_lag_keeps_its_own_threshold_untouched() -> None:
    """The existing input must behave exactly as before — production runs it."""
    guard = _guard(latency_warm_us=5_000, latency_storm_us=20_000)

    assert guard.update(latency_us=25_000) is StormGuardState.STORM


@pytest.mark.unit
def test_a_slow_broker_does_not_trip_the_loop_lag_threshold() -> None:
    """The whole point of the split: 400 ms at the broker is normal for that
    input and catastrophic for this one. They must not share a number."""
    guard = _guard(latency_warm_us=5_000, latency_storm_us=20_000)

    assert guard.update(order_rtt_us=400_000) is StormGuardState.NORMAL


@pytest.mark.unit
def test_the_reason_names_which_input_escalated() -> None:
    """`Latency 400000us` does not tell an operator whether to look at the
    engine or at the broker."""
    guard = _guard(order_rtt_warm_us=200_000, order_rtt_storm_us=500_000)

    _state, reason = guard._evaluate_target_state(drawdown_bps=0, latency_us=0, feed_gap_s=0.0, order_rtt_us=600_000)

    assert "rtt" in reason.lower()


# --------------------------------------------------------------------------- #
# Unarmed by default, and it says so                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_order_rtt_input_ships_unarmed() -> None:
    """No production samples exist to calibrate against; a guessed threshold on
    a risk breaker is worse than an explicit zero."""
    assert RiskThresholds().order_rtt_warm_us == 0
    assert RiskThresholds().order_rtt_storm_us == 0


@pytest.mark.unit
def test_an_unarmed_input_never_escalates_however_large_the_value() -> None:
    """Zero must mean "off", not "trips on everything"."""
    guard = _guard(order_rtt_warm_us=0, order_rtt_storm_us=0)

    assert guard.update(order_rtt_us=10_000_000) is StormGuardState.NORMAL


@pytest.mark.unit
def test_arming_is_reported_so_unarmed_is_not_read_as_quiet() -> None:
    """RC-4, the recurring one: a breaker that cannot fire looks exactly like a
    breaker with nothing to do."""
    guard = _guard(order_rtt_warm_us=0, order_rtt_storm_us=0)
    guard.publish_latency_input_health()

    assert guard.metrics.stormguard_latency_input_armed.labels(input="order_rtt")._value.get() == 0.0
    assert guard.metrics.stormguard_latency_input_armed.labels(input="loop_lag")._value.get() == 1.0


@pytest.mark.unit
def test_an_armed_order_rtt_input_reports_itself_armed() -> None:
    guard = _guard(order_rtt_warm_us=200_000, order_rtt_storm_us=500_000)
    guard.publish_latency_input_health()

    assert guard.metrics.stormguard_latency_input_armed.labels(input="order_rtt")._value.get() == 1.0


# --------------------------------------------------------------------------- #
# The headroom evidence the recalibration needs                                #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_each_input_publishes_the_largest_value_it_has_seen() -> None:
    """A threshold 1000x above its input is only visible if the input's range is
    visible. This is the number that shows a branch is dead — and, for order
    RTT, the number Charlie needs before arming anything."""
    guard = _guard(latency_warm_us=5_000_000, latency_storm_us=10_000_000)

    guard.update(latency_us=4_700)
    guard.update(latency_us=9_000)
    guard.update(latency_us=1_200)

    assert guard.metrics.stormguard_latency_input_max_us.labels(input="loop_lag")._value.get() == 9_000


@pytest.mark.unit
def test_the_observed_maximum_does_not_fall_back_down() -> None:
    """A max that decays is a max of the last sample, which tells you nothing
    about whether a threshold is reachable."""
    guard = _guard()

    guard.update(order_rtt_us=395_000)
    guard.update(order_rtt_us=12_000)

    assert guard.metrics.stormguard_latency_input_max_us.labels(input="order_rtt")._value.get() == 395_000


# --------------------------------------------------------------------------- #
# Environment configuration                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_order_rtt_thresholds_are_settable_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("HFT_STORMGUARD_ORDER_RTT_WARM_US", "150000")
    monkeypatch.setenv("HFT_STORMGUARD_ORDER_RTT_STORM_US", "400000")

    guard = StormGuard()

    assert guard.thresholds.order_rtt_warm_us == 150_000
    assert guard.thresholds.order_rtt_storm_us == 400_000


@pytest.mark.unit
def test_a_malformed_order_rtt_threshold_leaves_the_input_unarmed(monkeypatch) -> None:
    """Fail-closed on a risk threshold means "do not arm on a value you could
    not parse", not "fall back to something plausible"."""
    monkeypatch.setenv("HFT_STORMGUARD_ORDER_RTT_STORM_US", "400ms")

    guard = StormGuard()

    assert guard.thresholds.order_rtt_storm_us == 0


@pytest.mark.unit
def test_a_loop_lag_threshold_above_the_stall_watchdog_is_flagged_as_dead(monkeypatch, module_log_sink) -> None:
    """Provably unreachable: ``LoopStallWatchdog`` force-exits the process at
    ``HFT_LOOP_STALL_KILL_S`` (default 60 s), so a loop-lag threshold beyond
    that is killed before it can fire."""
    from hft_platform.risk import storm_guard

    events = module_log_sink(storm_guard)
    monkeypatch.setenv("HFT_LOOP_STALL_KILL_S", "60")
    monkeypatch.setenv("HFT_STORMGUARD_LATENCY_STORM_US", "90000000")  # 90 s

    StormGuard()

    assert any(entry.get("event") == "stormguard_latency_threshold_unreachable" for entry in events)


@pytest.mark.unit
def test_a_reachable_loop_lag_threshold_is_not_flagged(monkeypatch, module_log_sink) -> None:
    from hft_platform.risk import storm_guard

    events = module_log_sink(storm_guard)
    monkeypatch.setenv("HFT_LOOP_STALL_KILL_S", "60")
    monkeypatch.setenv("HFT_STORMGUARD_LATENCY_STORM_US", "20000")

    StormGuard()

    assert not any(entry.get("event") == "stormguard_latency_threshold_unreachable" for entry in events)


# --------------------------------------------------------------------------- #
# Nothing about the deployed configuration changes                             #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_existing_call_signature_still_works() -> None:
    """``system.py:1185`` passes three keywords; 170 test files build on that
    shape. Adding an input must not break any of them."""
    guard = _guard(latency_warm_us=5_000, latency_storm_us=20_000)

    assert guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0) is StormGuardState.NORMAL
