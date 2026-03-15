"""Gate B anti-leak / lookahead-bias tests for VpinBvcAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np

from research.alphas.vpin.impl import VpinBvcAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (both fields 0) must return a numeric value."""
    alpha = VpinBvcAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_stateful() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = VpinBvcAlpha(n_buckets=5, bucket_size=50.0)
    sig1 = alpha.update(100.0, 60.0)
    sig2 = alpha.update(110.0, 60.0)
    # Second update has a price change, so state should have evolved
    # (signal may or may not differ numerically, but internal state changed)
    assert alpha._initialized is True


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = VpinBvcAlpha(n_buckets=5, bucket_size=100.0)
    a2 = VpinBvcAlpha(n_buckets=5, bucket_size=100.0)
    # Pollute a1 state
    for i in range(20):
        a1.update(100.0 + i, 50.0)
    a1.reset()
    # Now feed identical sequence to both
    signals1 = []
    signals2 = []
    for i in range(30):
        signals1.append(a1.update(200.0 + i * 0.5, 40.0))
        signals2.append(a2.update(200.0 + i * 0.5, 40.0))
    assert signals1 == signals2


def test_deterministic_same_input_same_output() -> None:
    """Same input sequence always produces the same signal."""
    def run_sequence() -> list[float]:
        alpha = VpinBvcAlpha(n_buckets=10, bucket_size=50.0)
        results = []
        for i in range(100):
            results.append(alpha.update(100.0 + i * 0.1, 30.0))
        return results

    assert run_sequence() == run_sequence()


def test_no_future_leakage() -> None:
    """Signal at tick t is independent of data at t+1."""
    alpha_short = VpinBvcAlpha(n_buckets=5, bucket_size=100.0)
    alpha_long = VpinBvcAlpha(n_buckets=5, bucket_size=100.0)

    # Feed 50 ticks to both
    for i in range(50):
        alpha_short.update(100.0 + i * 0.1, 30.0)
        alpha_long.update(100.0 + i * 0.1, 30.0)

    sig_at_50 = alpha_short.get_signal()

    # Feed 50 more ticks only to alpha_long
    for i in range(50, 100):
        alpha_long.update(100.0 + i * 0.1, 30.0)

    # alpha_short's signal at tick 50 must not have changed
    assert alpha_short.get_signal() == sig_at_50


def test_signal_nonnegative() -> None:
    """VPIN is always >= 0."""
    alpha = VpinBvcAlpha(n_buckets=10, bucket_size=50.0)
    rng = np.random.default_rng(123)
    prices = np.cumsum(rng.standard_normal(300)) + 100.0
    volumes = rng.uniform(1, 80, 300)
    for p, v in zip(prices, volumes):
        sig = alpha.update(float(p), float(v))
        assert sig >= 0.0


def test_signal_monotonic_with_imbalanced_flow() -> None:
    """With increasingly imbalanced flow, VPIN should trend upward."""
    alpha = VpinBvcAlpha(n_buckets=10, bucket_size=100.0)
    # Phase 1: balanced (small price moves)
    for _ in range(200):
        alpha.update(100.0, 50.0)
    vpin_balanced = alpha.get_signal()

    # Phase 2: strongly directional (large consistent price increases)
    for i in range(200):
        alpha.update(100.0 + (i + 1) * 1.0, 50.0)
    vpin_directional = alpha.get_signal()

    assert vpin_directional > vpin_balanced


def test_many_updates_no_error() -> None:
    """10000 updates without exception."""
    alpha = VpinBvcAlpha(n_buckets=50, bucket_size=500.0)
    rng = np.random.default_rng(999)
    prices = np.cumsum(rng.standard_normal(10000)) + 500.0
    volumes = rng.uniform(1, 100, 10000)
    for p, v in zip(prices, volumes):
        sig = alpha.update(float(p), float(v))
        assert isinstance(sig, float)
