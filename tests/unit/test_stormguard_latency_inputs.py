"""The latency breaker measures the event loop and is calibrated for the broker.

``system.py:1159`` computes ``latency_us = int(lag_s * 1_000_000)`` — event-loop
lag, and nothing else. THESHOW runs it against
``HFT_STORMGUARD_LATENCY_WARM_US=5_000_000`` / ``STORM_US=10_000_000``, i.e. 5
and 10 *seconds*, while the measured distribution over 1,907,539 probes is
p50 ~0.55 ms, p99 ~4.7 ms, p99.9 ~9 ms, max ~1 s. The input is ~1000x below its
own threshold, so this branch of the breaker has never fired and cannot be
expected to. All 20 escalations in the 2-day window came from feed_gap and
drift_burst.

Two different quantities were being funnelled through one parameter: platform
health (loop lag, sub-millisecond, budget 1 ms) and trading-path health (order
round-trip, tens to hundreds of milliseconds — the broker profile measures
``place_order`` p95 at 395 ms). One threshold cannot serve both, and the one in
production serves neither.

This module splits them. The order-RTT input ships **unarmed**: THESHOW has no
order-RTT samples at all (``gateway_dispatch_latency_ns_count = 0``, gateway
disabled), so any threshold picked now would be a guess. Arming it is a
calibration decision with data behind it, not a default. What ships instead is
the evidence needed to make that decision: each input's observed maximum, and an
explicit armed/unarmed gauge so an unarmed breaker cannot be mistaken for a
quiet one.
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
def test_a_loop_lag_threshold_above_the_stall_watchdog_is_flagged_as_dead(monkeypatch) -> None:
    """Provably unreachable: ``LoopStallWatchdog`` force-exits the process at
    ``HFT_LOOP_STALL_KILL_S`` (default 60 s), so a loop-lag threshold beyond
    that is killed before it can fire."""
    from structlog.testing import capture_logs

    monkeypatch.setenv("HFT_LOOP_STALL_KILL_S", "60")
    monkeypatch.setenv("HFT_STORMGUARD_LATENCY_STORM_US", "90000000")  # 90 s

    with capture_logs() as logs:
        StormGuard()

    assert any(entry.get("event") == "stormguard_latency_threshold_unreachable" for entry in logs)


@pytest.mark.unit
def test_a_reachable_loop_lag_threshold_is_not_flagged(monkeypatch) -> None:
    from structlog.testing import capture_logs

    monkeypatch.setenv("HFT_LOOP_STALL_KILL_S", "60")
    monkeypatch.setenv("HFT_STORMGUARD_LATENCY_STORM_US", "20000")

    with capture_logs() as logs:
        StormGuard()

    assert not any(entry.get("event") == "stormguard_latency_threshold_unreachable" for entry in logs)


# --------------------------------------------------------------------------- #
# Nothing about the deployed configuration changes                             #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_existing_call_signature_still_works() -> None:
    """``system.py:1185`` passes three keywords; 170 test files build on that
    shape. Adding an input must not break any of them."""
    guard = _guard(latency_warm_us=5_000, latency_storm_us=20_000)

    assert guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0) is StormGuardState.NORMAL
