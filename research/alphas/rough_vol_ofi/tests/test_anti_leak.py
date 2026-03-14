"""Anti-leak tests for rough_vol_ofi signal (8 tests).

Tests verify:
- No future data contamination
- No division by zero on edge inputs
- No overflow on extreme inputs
- Correct signal direction after reset
- Deterministic replay
"""
from __future__ import annotations

import math

from research.alphas.rough_vol_ofi.impl import RoughVolOfiAlpha


def _make_alpha() -> RoughVolOfiAlpha:
    a = RoughVolOfiAlpha()
    a.reset()
    return a


# ---------------------------------------------------------------------------
# 1: Future data cannot affect past signal (causal tick replay)
# ---------------------------------------------------------------------------

def test_future_bids_do_not_affect_past_signal() -> None:
    """Replaying tick-by-tick, future ticks must not change earlier output."""
    ticks = [
        (100.0, 100.0), (150.0, 80.0), (90.0, 110.0),
        (200.0, 50.0), (60.0, 120.0), (180.0, 70.0),
    ]
    alpha = _make_alpha()
    signals: list[float] = []
    for b, a in ticks:
        signals.append(alpha.update(b, a))

    # Replay: same inputs must yield same outputs
    alpha.reset()
    for i, (b, a) in enumerate(ticks):
        s = alpha.update(b, a)
        assert abs(s - signals[i]) < 1e-12, (
            f"Signal at tick {i} changed on replay: {s} vs {signals[i]}"
        )


# ---------------------------------------------------------------------------
# 2-3: Zero-input edge cases (no division by zero)
# ---------------------------------------------------------------------------

def test_zero_bid_and_ask_no_division_by_zero() -> None:
    """bid_qty=ask_qty=0 must not raise and must return a finite float."""
    alpha = _make_alpha()
    alpha.update(100.0, 100.0)  # init
    result = alpha.update(0.0, 0.0)
    assert math.isfinite(result)


def test_zero_ask_qty_no_division_by_zero() -> None:
    """bid_qty > 0, ask_qty = 0 must not raise."""
    alpha = _make_alpha()
    alpha.update(100.0, 100.0)  # init
    result = alpha.update(100.0, 0.0)
    assert math.isfinite(result)


# ---------------------------------------------------------------------------
# 4-5: Extreme inputs -- no overflow
# ---------------------------------------------------------------------------

def test_extreme_large_input_no_overflow() -> None:
    """Extremely large bid/ask quantities must not produce inf or nan."""
    alpha = _make_alpha()
    for _ in range(50):
        result = alpha.update(1e15, 1e14)
        assert math.isfinite(result), f"Overflow with large input: {result}"


def test_extreme_alternating_no_overflow() -> None:
    """Alternating extreme values must keep signal finite and bounded."""
    alpha = _make_alpha()
    for i in range(200):
        if i % 2 == 0:
            result = alpha.update(1e12, 1.0)
        else:
            result = alpha.update(1.0, 1e12)
        assert math.isfinite(result), f"Non-finite signal at tick {i}: {result}"
        assert -1.0 <= result <= 1.0, f"Signal out of bounds at tick {i}: {result}"


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
    # Warmup with increasing bid (positive OFI)
    for i in range(200):
        alpha.update(100.0 + i, 100.0)
    assert alpha._ofi_ema > 0.0

    # Reset and feed decreasing bid (negative OFI)
    alpha.reset()
    for i in range(200):
        alpha.update(100.0, 100.0 + i)
    assert alpha._ofi_ema < 0.0, (
        "OFI EMA contaminated by pre-reset state: expected negative, "
        f"got {alpha._ofi_ema}"
    )


# ---------------------------------------------------------------------------
# 8: Hurst stays bounded even with pathological input
# ---------------------------------------------------------------------------

def test_hurst_ema_stays_bounded() -> None:
    """Hurst EMA must stay in [0, 1] even with wild inputs."""
    import numpy as np
    alpha = _make_alpha()
    rng = np.random.default_rng(42)
    for _ in range(2000):
        bid = float(rng.uniform(0.0, 1000.0))
        ask = float(rng.uniform(0.0, 1000.0))
        alpha.update(bid, ask)
        assert 0.0 <= alpha._hurst_ema <= 1.0, (
            f"Hurst EMA out of bounds: {alpha._hurst_ema}"
        )
