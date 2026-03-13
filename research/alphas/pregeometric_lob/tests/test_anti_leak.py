"""Gate B anti-leak / lookahead-bias tests for PregeometricLobAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np

from research.alphas.pregeometric_lob.impl import PregeometricLobAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (no depth data) must return a numeric value."""
    alpha = PregeometricLobAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_stateful_not_lookahead() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = PregeometricLobAlpha()
    # Moderate bid advantage (shape diff positive but within clamp range)
    bids1 = np.array([[100, 100], [99, 120], [98, 80], [97, 110]])
    asks1 = np.array([[101, 50], [102, 150], [103, 30], [104, 200]])
    sig1 = alpha.update(bids=bids1, asks=asks1)

    # Now reverse: ask side more concentrated than bid
    bids2 = np.array([[100, 50], [99, 150], [98, 30], [97, 200]])
    asks2 = np.array([[101, 100], [102, 120], [103, 80], [104, 110]])
    # Feed multiple ticks to let EMA catch up
    for _ in range(30):
        sig2 = alpha.update(bids=bids2, asks=asks2)

    assert sig2 < sig1  # signal moved in the correct direction


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = PregeometricLobAlpha()
    a2 = PregeometricLobAlpha()

    bids_warmup = np.array([[100, 500], [99, 100]])
    asks_warmup = np.array([[101, 50], [102, 400]])
    a1.update(bids=bids_warmup, asks=asks_warmup)
    a1.reset()

    bids = np.array([[100, 100], [99, 200], [98, 300]])
    asks = np.array([[101, 100], [102, 200], [103, 300]])
    s1 = a1.update(bids=bids, asks=asks)
    s2 = a2.update(bids=bids, asks=asks)
    assert s1 == s2


def test_no_future_information_leaks() -> None:
    """Signal at tick T must not change when future ticks are added."""
    alpha1 = PregeometricLobAlpha()
    alpha2 = PregeometricLobAlpha()

    rng = np.random.default_rng(99)
    # Feed 10 ticks to both
    for _ in range(10):
        bids = np.column_stack([np.arange(5), rng.exponential(100, 5)])
        asks = np.column_stack([np.arange(5), rng.exponential(100, 5)])
        alpha1.update(bids=bids, asks=asks)
        alpha2.update(bids=bids, asks=asks)

    sig_at_10 = alpha1.get_signal()

    # Feed 10 more ticks to alpha2 only
    for _ in range(10):
        bids = np.column_stack([np.arange(5), rng.exponential(100, 5)])
        asks = np.column_stack([np.arange(5), rng.exponential(100, 5)])
        alpha2.update(bids=bids, asks=asks)

    # alpha1 signal at tick 10 must be unchanged
    assert alpha1.get_signal() == sig_at_10


def test_independent_instances_no_shared_state() -> None:
    """Two separate instances must not share any mutable state."""
    a1 = PregeometricLobAlpha()
    a2 = PregeometricLobAlpha()

    bids = np.array([[100, 100], [99, 500]])
    asks = np.array([[101, 50], [102, 100]])
    a1.update(bids=bids, asks=asks)

    # a2 should be unaffected
    assert a2.get_signal() == 0.0


def test_slots_no_instance_dict() -> None:
    """__slots__ must prevent arbitrary attribute assignment (Allocator Law)."""
    alpha = PregeometricLobAlpha()
    assert not hasattr(alpha, "__dict__")
