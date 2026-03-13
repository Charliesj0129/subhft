"""Anti-leak tests for markov_lob_inertia signal (>=5 tests).

Tests verify:
- No future data contamination
- No division by zero on edge inputs
- No overflow on extreme inputs
- Correct signal direction after reset
- Reset idempotency
"""
from __future__ import annotations

import math

from research.alphas.markov_lob_inertia.impl import MarkovLobInertiaAlpha


def _make_alpha() -> MarkovLobInertiaAlpha:
    a = MarkovLobInertiaAlpha()
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

    # Replay: same inputs must yield same outputs
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
# 4: Extreme inputs -- no overflow
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
    for i in range(100):
        if i % 2 == 0:
            result = alpha.update(1e12, 1.0)
        else:
            result = alpha.update(1.0, 1e12)
        assert math.isfinite(result), f"Non-finite signal at tick {i}: {result}"


# ---------------------------------------------------------------------------
# 5: Signal bounds under random stress
# ---------------------------------------------------------------------------


def test_signal_bounded_under_random_stress() -> None:
    """Signal must stay in [-1.0, 1.0] for random input patterns."""
    import numpy as np

    alpha = _make_alpha()
    rng = np.random.default_rng(99)
    for _ in range(2000):
        bid = float(rng.uniform(0.0, 1000.0))
        ask = float(rng.uniform(0.0, 1000.0))
        s = alpha.update(bid, ask)
        assert -1.0 <= s <= 1.0, f"Signal out of bounds: {s}"


# ---------------------------------------------------------------------------
# 6: Reset idempotency
# ---------------------------------------------------------------------------


def test_two_resets_then_replay_identical() -> None:
    """Two independent runs from reset must yield identical signals."""
    alpha = _make_alpha()
    inputs = [(120.0, 80.0), (90.0, 110.0), (150.0, 60.0), (80.0, 130.0)]

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
# 7: Signal direction not contaminated after reset
# ---------------------------------------------------------------------------


def test_signal_direction_not_contaminated_after_reset() -> None:
    """Signal direction after reset must not be biased by prior warmup."""
    alpha = _make_alpha()
    # Warmup with increasing mid_price (UP state dominant)
    for i in range(200):
        alpha.update(100.0 + i, 50.0)
    assert alpha.get_signal() > 0.0

    # Reset and immediately feed decreasing pattern
    alpha.reset()
    for i in range(200):
        alpha.update(50.0, 100.0 + i)
    assert alpha.get_signal() < 0.0, (
        f"Signal contaminated by pre-reset state: expected negative, got {alpha.get_signal()}"
    )


# ---------------------------------------------------------------------------
# 8: Transition matrix stays finite under adversarial input
# ---------------------------------------------------------------------------


def test_transition_matrix_stays_finite() -> None:
    """All transition matrix entries must stay finite after many updates."""
    alpha = _make_alpha()
    for i in range(500):
        bid = 100.0 + (i % 7) * 50.0
        ask = 100.0 + ((i + 3) % 5) * 40.0
        alpha.update(bid, ask)
    for t_val in alpha._transitions:
        assert math.isfinite(t_val), f"Non-finite transition entry: {t_val}"
