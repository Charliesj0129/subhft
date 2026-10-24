"""Order-RTT STORM needs repeated slow orders, and holds until the broker is quiet.

From 10-04 to 10-23 the order-RTT input entered STORM 496 times; 335 of those
read 3000-3100 ms, the ``HFT_API_TIMEOUT_S=3.0`` timeout of a single broker
call. One timeout put the maker reduce-only for ~85 s, and the next one did it
again. STORM now needs ``order_rtt_storm_count`` slow orders within
``order_rtt_storm_window_s`` and ends only ``order_rtt_storm_dwell_s`` after
the last one.
"""

from __future__ import annotations

import pytest

from hft_platform.contracts.strategy import StormGuardState
from hft_platform.risk import storm_guard as storm_guard_module
from hft_platform.risk.storm_guard import RiskThresholds, StormGuard

TIMEOUT_US = 3_000_000  # what a timed-out place_order reports


class _Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(storm_guard_module.time, "monotonic", c)
    return c


def _quiet_ticks(guard: StormGuard, clock: _Clock, n: int, step_s: float = 1.0) -> StormGuardState:
    state = guard.state
    for _ in range(n):
        clock.now += step_s
        state = guard.update()
    return state


def test_one_timed_out_order_warns_and_does_not_stop_quoting(clock) -> None:
    guard = StormGuard()

    guard.observe_order_rtt_us(TIMEOUT_US)

    assert guard.update() is StormGuardState.WARM


def test_three_slow_orders_within_a_minute_enter_storm(clock) -> None:
    guard = StormGuard()

    for _ in range(2):
        guard.observe_order_rtt_us(TIMEOUT_US)
        clock.now += 20
        assert guard.update() is not StormGuardState.STORM
    guard.observe_order_rtt_us(TIMEOUT_US)

    assert guard.update() is StormGuardState.STORM
    assert guard.state_reason.startswith("Order RTT")


def test_three_slow_orders_spread_over_more_than_a_minute_stay_out_of_storm(clock) -> None:
    guard = StormGuard()

    for _ in range(3):
        guard.observe_order_rtt_us(TIMEOUT_US)
        assert guard.update() is not StormGuardState.STORM
        clock.now += 31

    assert guard.state is not StormGuardState.STORM


def _enter_storm(guard: StormGuard, clock: _Clock) -> None:
    for _ in range(3):
        guard.observe_order_rtt_us(TIMEOUT_US)
        clock.now += 1
    assert guard.update() is StormGuardState.STORM


def test_storm_holds_for_the_dwell_after_the_last_slow_order(clock) -> None:
    guard = StormGuard()
    _enter_storm(guard, clock)

    # 299 s of quiet ticks: still STORM.
    assert _quiet_ticks(guard, clock, 299) is StormGuardState.STORM
    # Past the 300 s dwell, the usual N clean ticks bring it down.
    assert _quiet_ticks(guard, clock, 10) is StormGuardState.NORMAL


def test_a_slow_order_during_the_dwell_restarts_it(clock) -> None:
    guard = StormGuard()
    _enter_storm(guard, clock)

    _quiet_ticks(guard, clock, 200)
    guard.observe_order_rtt_us(TIMEOUT_US)
    clock.now += 1
    assert guard.update() is StormGuardState.STORM

    # 200 s after the restart: the old dwell would have ended, the new one has not.
    assert _quiet_ticks(guard, clock, 200) is StormGuardState.STORM
    assert _quiet_ticks(guard, clock, 110) is StormGuardState.NORMAL


def test_the_host_can_set_count_window_and_dwell(clock, monkeypatch) -> None:
    monkeypatch.setenv("HFT_STORMGUARD_ORDER_RTT_STORM_COUNT", "2")
    monkeypatch.setenv("HFT_STORMGUARD_ORDER_RTT_STORM_WINDOW_S", "10")
    monkeypatch.setenv("HFT_STORMGUARD_ORDER_RTT_STORM_DWELL_S", "60")
    guard = StormGuard()

    guard.observe_order_rtt_us(TIMEOUT_US)
    guard.observe_order_rtt_us(TIMEOUT_US)

    assert guard.update() is StormGuardState.STORM
    assert _quiet_ticks(guard, clock, 59) is StormGuardState.STORM
    assert _quiet_ticks(guard, clock, 10) is StormGuardState.NORMAL


@pytest.mark.parametrize("raw", ["0", "-1", "three"])
def test_an_invalid_count_keeps_the_default(clock, monkeypatch, raw) -> None:
    monkeypatch.setenv("HFT_STORMGUARD_ORDER_RTT_STORM_COUNT", raw)

    guard = StormGuard()

    assert guard.thresholds.order_rtt_storm_count == RiskThresholds().order_rtt_storm_count == 3


def test_the_defaults_are_three_in_sixty_seconds_held_three_hundred() -> None:
    t = RiskThresholds()

    assert (t.order_rtt_storm_count, t.order_rtt_storm_window_s, t.order_rtt_storm_dwell_s) == (3, 60.0, 300.0)
