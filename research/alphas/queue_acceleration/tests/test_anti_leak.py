"""Gate B anti-leak / lookahead-bias tests for QueueAccelerationAlpha."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np

from research.alphas.queue_acceleration.impl import QueueAccelerationAlpha


def _make_alpha() -> QueueAccelerationAlpha:
    a = QueueAccelerationAlpha()
    a.reset()
    return a


# ---------------------------------------------------------------------------
# 1: Replay determinism (no future data leakage)
# ---------------------------------------------------------------------------


def test_replay_determinism() -> None:
    """Replaying tick-by-tick, same inputs must yield identical outputs."""
    ticks = [(100.0, 100.0), (150.0, 80.0), (90.0, 110.0), (200.0, 50.0), (60.0, 120.0)]
    alpha = _make_alpha()
    signals: list[float] = []
    for b, a in ticks:
        signals.append(alpha.update(b, a))

    alpha.reset()
    for i, (b, a) in enumerate(ticks):
        s = alpha.update(b, a)
        assert abs(s - signals[i]) < 1e-12, f"Signal at tick {i} changed on replay: {s} vs {signals[i]}"


# ---------------------------------------------------------------------------
# 2-3: Zero-input edge cases
# ---------------------------------------------------------------------------


def test_zero_bid_and_ask_no_division_by_zero() -> None:
    """bid_qty=ask_qty=0 must not raise and must return a finite float."""
    alpha = _make_alpha()
    result = alpha.update(0.0, 0.0)
    assert math.isfinite(result)


def test_zero_ask_qty_no_division_by_zero() -> None:
    """bid_qty > 0, ask_qty = 0 must not raise."""
    alpha = _make_alpha()
    result = alpha.update(100.0, 0.0)
    assert math.isfinite(result)


# ---------------------------------------------------------------------------
# 4-5: Extreme inputs — no overflow
# ---------------------------------------------------------------------------


def test_extreme_large_input_no_overflow() -> None:
    """Extremely large bid/ask quantities must not produce inf or nan."""
    alpha = _make_alpha()
    for _ in range(50):
        result = alpha.update(1e15, 1e14)
        assert math.isfinite(result), f"Overflow with large input: {result}"


def test_extreme_alternating_no_overflow() -> None:
    """Alternating extreme values must keep signal finite."""
    alpha = _make_alpha()
    for i in range(200):
        if i % 2 == 0:
            result = alpha.update(1e12, 1.0)
        else:
            result = alpha.update(1.0, 1e12)
        assert math.isfinite(result), f"Non-finite signal at tick {i}: {result}"


# ---------------------------------------------------------------------------
# 6: Signal direction
# ---------------------------------------------------------------------------


def test_ramp_bid_gives_positive_then_decay() -> None:
    """Ramping bid dominance produces positive accel; constant input decays."""
    alpha = _make_alpha()
    for _ in range(20):
        alpha.update(100.0, 100.0)
    # Ramp up bid
    for i in range(20):
        alpha.update(100.0 + i * 20.0, 100.0)
    sig_during_ramp = alpha.get_signal()
    # Hold constant — accel should decay toward zero
    for _ in range(200):
        alpha.update(500.0, 100.0)
    sig_after_hold = alpha.get_signal()
    assert sig_during_ramp > sig_after_hold


# ---------------------------------------------------------------------------
# 7: Signal bounds under random stress
# ---------------------------------------------------------------------------


def test_signal_bounded_random_stress() -> None:
    """Signal must stay in [-1, 1] under random input stress."""
    alpha = _make_alpha()
    rng = np.random.default_rng(99)
    for _ in range(2000):
        bid = float(rng.uniform(0.0, 1000.0))
        ask = float(rng.uniform(0.0, 1000.0))
        s = alpha.update(bid, ask)
        assert -1.0 <= s <= 1.0, f"Signal out of bounds: {s}"


# ---------------------------------------------------------------------------
# 8: Reset idempotency
# ---------------------------------------------------------------------------


def test_two_resets_then_replay_identical() -> None:
    """Two independent runs from reset must yield identical signals."""
    alpha = _make_alpha()
    inputs = [(120.0, 80.0), (90.0, 110.0), (150.0, 60.0), (80.0, 200.0)]

    run1: list[float] = []
    alpha.reset()
    for b, a in inputs:
        run1.append(alpha.update(b, a))

    run2: list[float] = []
    alpha.reset()
    for b, a in inputs:
        run2.append(alpha.update(b, a))

    for i, (s1, s2) in enumerate(zip(run1, run2)):
        assert abs(s1 - s2) < 1e-14, f"Tick {i}: {s1} vs {s2}"


# ---------------------------------------------------------------------------
# 9: Signal direction not contaminated after reset
# ---------------------------------------------------------------------------


def test_signal_direction_not_contaminated_after_reset() -> None:
    """Signal direction after reset must not be biased by prior warmup."""
    alpha = _make_alpha()
    # Warmup with accelerating bid dominance (quadratic ramp)
    for i in range(50):
        alpha.update(100.0 + i * i * 0.5, 100.0)

    # Reset and feed a fresh independent sequence — two fresh alphas should match
    alpha.reset()
    fresh = _make_alpha()
    inputs = [(100.0, 100.0)] * 10 + [(100.0, 100.0 + i * 10.0) for i in range(30)]
    for b, a in inputs:
        alpha.update(b, a)
        fresh.update(b, a)
    assert abs(alpha.get_signal() - fresh.get_signal()) < 1e-12, (
        f"Signal contaminated by pre-reset state: reset={alpha.get_signal()}, fresh={fresh.get_signal()}"
    )
