"""Anti-leak tests for regime_momentum signal (>=8 tests).

Tests verify:
- No future data contamination
- No division by zero on edge inputs
- No overflow on extreme inputs
- Correct signal direction
- Reset idempotency
- No state contamination across resets
"""

from __future__ import annotations

import math

from research.alphas.regime_momentum.impl import RegimeMomentumAlpha


def _make_alpha() -> RegimeMomentumAlpha:
    a = RegimeMomentumAlpha()
    a.reset()
    return a


# ---------------------------------------------------------------------------
# 1: Future data cannot affect past signal (causal tick replay)
# ---------------------------------------------------------------------------


def test_future_bids_do_not_affect_past_signal() -> None:
    """Replaying tick-by-tick, future ticks must not change earlier output."""
    ticks = [(100.0, 100.0), (150.0, 80.0), (90.0, 110.0), (200.0, 50.0), (60.0, 120.0)]
    alpha = _make_alpha()
    signals: list[float] = []
    for b, a in ticks:
        signals.append(alpha.update(b, a))

    # Replay again: same inputs must yield same outputs
    alpha.reset()
    for i, (b, a) in enumerate(ticks):
        s = alpha.update(b, a)
        assert abs(s - signals[i]) < 1e-12, f"Signal at tick {i} changed on replay: {s} vs {signals[i]}"


# ---------------------------------------------------------------------------
# 2-3: Zero-input edge cases (no division by zero)
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
# 4: Extreme inputs — no overflow
# ---------------------------------------------------------------------------


def test_extreme_large_input_no_overflow() -> None:
    """Extremely large bid/ask quantities must not produce inf or nan."""
    alpha = _make_alpha()
    for _ in range(20):
        result = alpha.update(1e15, 1e14)
        assert math.isfinite(result), f"Overflow with large input: {result}"


def test_extreme_alternating_no_overflow() -> None:
    """Alternating extreme values must keep signal finite."""
    alpha = _make_alpha()
    for i in range(100):
        if i % 2 == 0:
            result = alpha.update(1e12, 1.0)
        else:
            result = alpha.update(1.0, 1e12)
        assert math.isfinite(result), f"Non-finite signal at tick {i}: {result}"


# ---------------------------------------------------------------------------
# 5: Signal direction correctness over 200 ticks
# ---------------------------------------------------------------------------


def test_strong_bid_dominance_gives_positive_signal_during_transition() -> None:
    """Vol regime shift with bid dominance: signal positive during transition.

    regime_momentum measures rf momentum (fast-slow EMA delta), so signal is
    strongest during regime transitions, not in steady state.
    """
    alpha = _make_alpha()
    # Warmup with balanced (low vol)
    for _ in range(200):
        alpha.update(100.0, 100.0)
    # Transition: sudden bid dominance -> vol spikes -> rf_ema8 > rf_ema32
    for _ in range(15):
        alpha.update(300.0, 50.0)
    assert alpha.get_signal() > 0.0, f"Expected positive signal, got {alpha.get_signal()}"


def test_strong_ask_dominance_gives_negative_signal_during_transition() -> None:
    """Vol regime shift with ask dominance: signal negative during transition."""
    alpha = _make_alpha()
    for _ in range(200):
        alpha.update(100.0, 100.0)
    for _ in range(15):
        alpha.update(50.0, 300.0)
    assert alpha.get_signal() < 0.0, f"Expected negative signal, got {alpha.get_signal()}"


# ---------------------------------------------------------------------------
# 6: Signal bounds with 1000 random ticks
# ---------------------------------------------------------------------------


def test_signal_bounded_within_minus_2_to_plus_2() -> None:
    """Signal must stay in [-2.0, 2.0] for any input pattern."""
    import numpy as np

    alpha = _make_alpha()
    rng = np.random.default_rng(99)
    for _ in range(1000):
        bid = float(rng.uniform(0.0, 500.0))
        ask = float(rng.uniform(0.0, 500.0))
        s = alpha.update(bid, ask)
        assert -2.0 <= s <= 2.0, f"Signal out of bounds: {s}"


# ---------------------------------------------------------------------------
# 7: Reset idempotency
# ---------------------------------------------------------------------------


def test_two_resets_then_replay_identical() -> None:
    """Two independent runs from reset must yield identical signals."""
    alpha = _make_alpha()
    inputs = [(120.0, 80.0), (90.0, 110.0), (150.0, 60.0)]

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
# 8: Signal does not reverse direction after reset (no contamination)
# ---------------------------------------------------------------------------


def test_signal_direction_not_contaminated_after_reset() -> None:
    """Signal direction after reset must not be biased by prior warmup.

    We test during the transition window (not converged steady state)
    because regime_momentum measures rf delta, which converges to 0.
    """
    alpha = _make_alpha()
    # Warmup with balanced then transition to bid dominance
    for _ in range(100):
        alpha.update(100.0, 100.0)
    for _ in range(15):
        alpha.update(500.0, 10.0)
    assert alpha.get_signal() > 0.0

    # Reset and apply balanced then ask dominance transition
    alpha.reset()
    for _ in range(100):
        alpha.update(100.0, 100.0)
    for _ in range(15):
        alpha.update(10.0, 500.0)
    assert alpha.get_signal() < 0.0, (
        f"Signal contaminated by pre-reset state: expected negative, got {alpha.get_signal()}"
    )
