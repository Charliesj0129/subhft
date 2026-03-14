"""Gate B anti-leak / lookahead-bias tests for FlowPersistenceAlpha (ref 089)."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.flow_persistence.impl import FlowPersistenceAlpha

# ---------------------------------------------------------------------------
# 1. Replay determinism
# ---------------------------------------------------------------------------


def test_replay_determinism() -> None:
    """Two fresh instances fed the same sequence produce identical signals."""
    a1 = FlowPersistenceAlpha()
    a2 = FlowPersistenceAlpha()
    rng = np.random.default_rng(99)
    bids = rng.uniform(0, 500, 100)
    asks = rng.uniform(0, 500, 100)
    for b, a in zip(bids, asks):
        s1 = a1.update(b, a)
        s2 = a2.update(b, a)
        assert s1 == s2


# ---------------------------------------------------------------------------
# 2. Zero input
# ---------------------------------------------------------------------------


def test_update_no_args_returns_float() -> None:
    """update() with no args (both queues 0) must return a numeric value."""
    alpha = FlowPersistenceAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))
    assert result == pytest.approx(0.0, abs=1e-12)


# ---------------------------------------------------------------------------
# 3. Extreme values
# ---------------------------------------------------------------------------


def test_extreme_bid_stays_bounded() -> None:
    """Extreme bid values should not blow up signal."""
    alpha = FlowPersistenceAlpha()
    for _ in range(100):
        sig = alpha.update(1e12, 1.0)
        assert -1.0 <= sig <= 1.0


def test_extreme_ask_stays_bounded() -> None:
    """Extreme ask values should not blow up signal."""
    alpha = FlowPersistenceAlpha()
    for _ in range(100):
        sig = alpha.update(1.0, 1e12)
        assert -1.0 <= sig <= 1.0


# ---------------------------------------------------------------------------
# 4. Alternating input (disagreement detection)
# ---------------------------------------------------------------------------


def test_alternating_produces_negative_agreement() -> None:
    """Rapid alternation between bid/ask dominance should produce near-zero
    or negative agreement (fast and slow have opposite signs)."""
    alpha = FlowPersistenceAlpha()
    # Alternate strongly between bid-heavy and ask-heavy
    for i in range(200):
        if i % 2 == 0:
            alpha.update(500.0, 100.0)
        else:
            alpha.update(100.0, 500.0)
    # With rapid alternation, fast EMA oscillates more while slow stays near 0
    # Agreement should be near zero or negative
    sig = alpha.get_signal()
    assert abs(sig) < 0.1, f"Expected near-zero signal from alternating, got {sig}"


# ---------------------------------------------------------------------------
# 5. Sustained direction (200 ticks)
# ---------------------------------------------------------------------------


def test_sustained_direction_200_ticks() -> None:
    """200 ticks of sustained bid dominance should produce positive signal."""
    alpha = FlowPersistenceAlpha()
    for _ in range(200):
        alpha.update(600.0, 200.0)
    assert alpha.get_signal() > 0.05


# ---------------------------------------------------------------------------
# 6. Bounds with random data (1000 ticks)
# ---------------------------------------------------------------------------


def test_bounds_1000_random_ticks() -> None:
    """Signal stays in [-1, 1] across 1000 random ticks."""
    alpha = FlowPersistenceAlpha()
    rng = np.random.default_rng(1234)
    for _ in range(1000):
        b = rng.uniform(0, 10000)
        a = rng.uniform(0, 10000)
        sig = alpha.update(b, a)
        assert -1.0 <= sig <= 1.0


# ---------------------------------------------------------------------------
# 7. Reset idempotency
# ---------------------------------------------------------------------------


def test_reset_idempotency() -> None:
    """Multiple resets should not change behavior."""
    alpha = FlowPersistenceAlpha()
    alpha.update(500.0, 100.0)
    alpha.update(500.0, 100.0)
    alpha.reset()
    alpha.reset()
    alpha.reset()
    sig = alpha.update(300.0, 300.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_reset_then_same_sequence() -> None:
    """After reset, feeding the same sequence as a fresh instance gives same result."""
    a1 = FlowPersistenceAlpha()
    a2 = FlowPersistenceAlpha()
    # Dirty a1
    a1.update(800.0, 200.0)
    a1.update(200.0, 800.0)
    a1.reset()
    # Now feed both the same sequence
    s1 = a1.update(300.0, 100.0)
    s2 = a2.update(300.0, 100.0)
    assert s1 == s2


# ---------------------------------------------------------------------------
# 8. No contamination between instances
# ---------------------------------------------------------------------------


def test_no_contamination_between_instances() -> None:
    """Updating one instance should not affect another."""
    a1 = FlowPersistenceAlpha()
    a2 = FlowPersistenceAlpha()
    a1.update(900.0, 100.0)
    a1.update(900.0, 100.0)
    assert a2.get_signal() == 0.0
    s2 = a2.update(300.0, 300.0)
    assert s2 == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# 9. Stateful, not lookahead
# ---------------------------------------------------------------------------


def test_update_is_stateful_not_lookahead() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = FlowPersistenceAlpha()
    sig1 = alpha.update(500.0, 100.0)
    sig2 = alpha.update(100.0, 500.0)
    # After a reversal, signal should change
    assert sig1 != sig2
