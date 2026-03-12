"""Gate B anti-leak / lookahead-bias tests for PriceLevelRevertAlpha.

Tests verify:
- No future data contamination (causal replay)
- Reset eliminates state dependency
- No global mutable state shared between instances
- Input order matters (no lookahead)
- No lookahead in signal computation
"""
from __future__ import annotations

import math

import pytest

from research.alphas.price_level_revert.impl import PriceLevelRevertAlpha


def _make_alpha() -> PriceLevelRevertAlpha:
    a = PriceLevelRevertAlpha()
    a.reset()
    return a


# ---------------------------------------------------------------------------
# 1: No future data leakage (causal tick replay)
# ---------------------------------------------------------------------------


def test_no_future_data_leakage() -> None:
    """Replaying tick-by-tick, future ticks must not change earlier output."""
    ticks = [
        (100000, 100),
        (105000, 120),
        (95000, 80),
        (110000, 150),
        (98000, 90),
    ]
    alpha = _make_alpha()
    signals: list[float] = []
    for mid, spread in ticks:
        signals.append(alpha.update(mid, spread))

    # Replay: same inputs must yield same outputs
    alpha.reset()
    for i, (mid, spread) in enumerate(ticks):
        s = alpha.update(mid, spread)
        assert abs(s - signals[i]) < 1e-12, (
            f"Signal at tick {i} changed on replay: {s} vs {signals[i]}"
        )


# ---------------------------------------------------------------------------
# 2: Reset eliminates state dependency
# ---------------------------------------------------------------------------


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = PriceLevelRevertAlpha()
    a2 = PriceLevelRevertAlpha()
    # Contaminate a1 with different history
    a1.update(200000, 50)
    a1.update(50000, 200)
    a1.reset()
    # Both should produce identical results
    s1 = a1.update(100000, 100)
    s2 = a2.update(100000, 100)
    assert s1 == pytest.approx(s2, abs=1e-12)


# ---------------------------------------------------------------------------
# 3: No global mutable state between instances
# ---------------------------------------------------------------------------


def test_no_global_mutable_state() -> None:
    """Two independent instances must not share state."""
    a1 = _make_alpha()
    a2 = _make_alpha()
    # Feed different data to each
    a1.update(200000, 100)
    a2.update(50000, 100)
    # After warmup, signals should differ
    for _ in range(100):
        a1.update(200000, 100)
        a2.update(50000, 100)
    # Both converge to 0 with constant input, but intermediate signals differ
    a1.reset()
    a2.reset()
    a1.update(100000, 100)
    s1 = a1.update(120000, 100)
    a2.update(100000, 100)
    s2 = a2.update(120000, 100)
    assert s1 == pytest.approx(s2, abs=1e-12), "Instances should not share state"


# ---------------------------------------------------------------------------
# 4: Input order matters (no lookahead)
# ---------------------------------------------------------------------------


def test_order_matters() -> None:
    """Swapping input order should produce different intermediate signals."""
    a1 = _make_alpha()
    a2 = _make_alpha()
    # Same ticks, different order
    a1.update(100000, 100)
    s1 = a1.update(120000, 100)

    a2.update(120000, 100)
    s2 = a2.update(100000, 100)

    # Different ordering should produce different signals
    assert s1 != pytest.approx(s2, abs=1e-10), (
        "Different input ordering produced identical signals — possible lookahead"
    )


# ---------------------------------------------------------------------------
# 5: No lookahead in signal (signal only depends on past)
# ---------------------------------------------------------------------------


def test_no_lookahead_in_signal() -> None:
    """Signal at tick N must be deterministic from ticks 0..N only."""
    ticks = [
        (100000, 100),
        (105000, 110),
        (103000, 95),
    ]
    # Run full sequence
    alpha_full = _make_alpha()
    signals_full: list[float] = []
    for mid, spread in ticks:
        signals_full.append(alpha_full.update(mid, spread))

    # Run partial (first 2 ticks only)
    alpha_partial = _make_alpha()
    for mid, spread in ticks[:2]:
        alpha_partial.update(mid, spread)

    # Signal at tick 1 must be same regardless of whether tick 2 was processed
    assert signals_full[1] == pytest.approx(alpha_partial.get_signal(), abs=1e-12)


# ---------------------------------------------------------------------------
# 6: Edge cases — zero/extreme inputs remain finite
# ---------------------------------------------------------------------------


def test_zero_spread_floor() -> None:
    """spread_scaled=0 should be floored to 1, not cause division by zero."""
    alpha = _make_alpha()
    result = alpha.update(100000, 0)
    assert math.isfinite(result)


def test_extreme_inputs_no_overflow() -> None:
    """Extremely large mid_price_x2 values must not produce inf or nan."""
    alpha = _make_alpha()
    for _ in range(50):
        result = alpha.update(1e15, 1)
        assert math.isfinite(result), f"Overflow with large input: {result}"


# ---------------------------------------------------------------------------
# 7: Reset idempotency
# ---------------------------------------------------------------------------


def test_two_resets_then_replay_identical() -> None:
    """Two independent runs from reset must yield identical signals."""
    alpha = _make_alpha()
    inputs = [(100000, 100), (110000, 120), (95000, 80)]

    run1: list[float] = []
    alpha.reset()
    for mid, spread in inputs:
        run1.append(alpha.update(mid, spread))

    run2: list[float] = []
    alpha.reset()
    for mid, spread in inputs:
        run2.append(alpha.update(mid, spread))

    for i, (s1, s2) in enumerate(zip(run1, run2)):
        assert abs(s1 - s2) < 1e-14, f"Tick {i}: {s1} vs {s2}"


# ---------------------------------------------------------------------------
# 8: Signal direction not contaminated after reset
# ---------------------------------------------------------------------------


def test_signal_direction_not_contaminated_after_reset() -> None:
    """Signal direction after reset must not be biased by prior warmup."""
    alpha = _make_alpha()
    # Warmup with price above average pattern
    for _ in range(200):
        alpha.update(100000, 100)
    for _ in range(50):
        alpha.update(120000, 100)
    assert alpha.get_signal() < 0.0  # price above avg -> negative

    # Reset and immediately feed price-below-avg pattern
    alpha.reset()
    for _ in range(200):
        alpha.update(100000, 100)
    for _ in range(50):
        alpha.update(80000, 100)
    assert alpha.get_signal() > 0.0, (
        "Signal contaminated by pre-reset state: expected positive, "
        f"got {alpha.get_signal()}"
    )
