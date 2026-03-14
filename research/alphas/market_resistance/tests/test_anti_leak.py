"""Anti-leak / lookahead-bias tests for MarketResistanceAlpha (ref 098).

Tests verify:
- No future data contamination
- No division by zero on edge inputs
- No overflow on extreme inputs
- Correct causal replay
- Reset idempotency
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from research.alphas.market_resistance.impl import MarketResistanceAlpha


def _make_alpha() -> MarketResistanceAlpha:
    a = MarketResistanceAlpha()
    a.reset()
    return a


# ---------------------------------------------------------------------------
# 1: Future data cannot affect past signal (causal tick replay)
# ---------------------------------------------------------------------------


def test_future_ticks_do_not_affect_past_signal() -> None:
    """Replaying tick-by-tick, future ticks must not change earlier output."""
    ticks = [
        (100.0, 100.0, 50.0),
        (150.0, 80.0, 50.1),
        (90.0, 110.0, 49.9),
        (200.0, 50.0, 50.3),
        (60.0, 120.0, 49.8),
    ]
    alpha = _make_alpha()
    signals: list[float] = []
    for b, a, m in ticks:
        signals.append(alpha.update(b, a, m))

    # Replay: same inputs must yield same outputs
    alpha.reset()
    for i, (b, a, m) in enumerate(ticks):
        s = alpha.update(b, a, m)
        assert abs(s - signals[i]) < 1e-12, (
            f"Signal at tick {i} changed on replay: {s} vs {signals[i]}"
        )


# ---------------------------------------------------------------------------
# 2-3: Zero-input edge cases (no division by zero)
# ---------------------------------------------------------------------------


def test_zero_bid_and_ask_no_division_by_zero() -> None:
    """bid_qty=ask_qty=0, mid_price=0 must not raise and must return finite."""
    alpha = _make_alpha()
    result = alpha.update(0.0, 0.0, 0.0)
    assert math.isfinite(result)


def test_zero_price_change_no_division_by_zero() -> None:
    """Zero price movement (flat mid) must not cause division by zero."""
    alpha = _make_alpha()
    for _ in range(50):
        result = alpha.update(100.0, 50.0, 100.0)
        assert math.isfinite(result)


# ---------------------------------------------------------------------------
# 4: Extreme inputs — no overflow
# ---------------------------------------------------------------------------


def test_extreme_large_input_no_overflow() -> None:
    """Extremely large bid/ask quantities must not produce inf or nan."""
    alpha = _make_alpha()
    for _ in range(50):
        result = alpha.update(1e15, 1e14, 1e6)
        assert math.isfinite(result), f"Overflow with large input: {result}"


def test_extreme_alternating_no_overflow() -> None:
    """Alternating extreme values must keep signal finite."""
    alpha = _make_alpha()
    for i in range(100):
        if i % 2 == 0:
            result = alpha.update(1e12, 1.0, 100.0 + i * 0.01)
        else:
            result = alpha.update(1.0, 1e12, 100.0 - i * 0.01)
        assert math.isfinite(result), f"Non-finite signal at tick {i}: {result}"


# ---------------------------------------------------------------------------
# 5: Reset idempotency
# ---------------------------------------------------------------------------


def test_two_resets_then_replay_identical() -> None:
    """Two independent runs from reset must yield identical signals."""
    alpha = _make_alpha()
    inputs = [(120.0, 80.0, 50.0), (90.0, 110.0, 49.5), (150.0, 60.0, 50.5)]

    run1: list[float] = []
    alpha.reset()
    for b, a, m in inputs:
        run1.append(alpha.update(b, a, m))

    run2: list[float] = []
    alpha.reset()
    for b, a, m in inputs:
        run2.append(alpha.update(b, a, m))

    for i, (s1, s2) in enumerate(zip(run1, run2)):
        assert abs(s1 - s2) < 1e-14, f"Tick {i}: {s1} vs {s2}"


def test_signal_direction_not_contaminated_after_reset() -> None:
    """Signal direction after reset must not be biased by prior warmup."""
    alpha = _make_alpha()
    mid = 100.0
    bid = 100.0
    # Warmup with strong bid growth (positive OFI, flat price -> positive signal)
    for _ in range(200):
        bid += 5.0
        alpha.update(bid, 100.0, mid)
    assert alpha.get_signal() > 0.0

    # Reset and feed ask growth (negative OFI, flat price -> negative signal)
    alpha.reset()
    ask = 100.0
    mid2 = 100.0
    for _ in range(200):
        ask += 5.0
        alpha.update(100.0, ask, mid2)
    assert alpha.get_signal() < 0.0, (
        "Signal contaminated by pre-reset state: expected negative, "
        f"got {alpha.get_signal()}"
    )


# ---------------------------------------------------------------------------
# 6: Signal bounds under random stress
# ---------------------------------------------------------------------------


def test_signal_bounded_within_minus_2_to_plus_2() -> None:
    """Signal must stay in [-2.0, 2.0] for any input pattern."""
    alpha = _make_alpha()
    rng = np.random.default_rng(99)
    for _ in range(1000):
        bid = float(rng.uniform(0.0, 500.0))
        ask = float(rng.uniform(0.0, 500.0))
        mid = float(rng.uniform(90.0, 110.0))
        s = alpha.update(bid, ask, mid)
        assert -2.0 <= s <= 2.0, f"Signal out of bounds: {s}"
