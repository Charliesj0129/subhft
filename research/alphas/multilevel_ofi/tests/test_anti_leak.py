"""Gate B anti-leak / lookahead-bias tests for MultilevelOfiAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np

from research.alphas.multilevel_ofi.impl import MultilevelOfiAlpha


def _make_depth(
    bid_qtys: list[float], ask_qtys: list[float]
) -> tuple[np.ndarray, np.ndarray]:
    """Helper: build 5-level bids/asks arrays from qty lists."""
    bids = np.zeros((5, 2), dtype=np.float64)
    asks = np.zeros((5, 2), dtype=np.float64)
    for i, q in enumerate(bid_qtys[:5]):
        bids[i] = [100 - i, q]
    for i, q in enumerate(ask_qtys[:5]):
        asks[i] = [101 + i, q]
    return bids, asks


def test_update_no_args_returns_float() -> None:
    """update() with no args (all zero depth) must return a numeric value."""
    alpha = MultilevelOfiAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_stateful_not_lookahead() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = MultilevelOfiAlpha()
    # Use small quantities to avoid hitting the [-2, 2] clip boundary
    bids1, asks1 = _make_depth([0.5, 0.4, 0.3, 0.2, 0.1], [0.1, 0.08, 0.06, 0.04, 0.02])
    bids2, asks2 = _make_depth([0.1, 0.08, 0.06, 0.04, 0.02], [0.5, 0.4, 0.3, 0.2, 0.1])
    sig1 = alpha.update(bids=bids1, asks=asks1)
    sig2 = alpha.update(bids=bids2, asks=asks2)
    # sig1 reflects initial bid-heavy depth from zero
    assert sig1 > 0.0
    # sig2 should drop (ask-heavy change) -- signal moved in correct direction
    assert sig2 < sig1


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = MultilevelOfiAlpha()
    a2 = MultilevelOfiAlpha()
    bids_warm, asks_warm = _make_depth([800, 600, 400, 200, 100], [200, 150, 100, 50, 25])
    a1.update(bids=bids_warm, asks=asks_warm)
    a1.reset()
    bids_test, asks_test = _make_depth([300, 250, 200, 150, 100], [300, 250, 200, 150, 100])
    s1 = a1.update(bids=bids_test, asks=asks_test)
    s2 = a2.update(bids=bids_test, asks=asks_test)
    assert s1 == s2


def test_signal_depends_on_delta_not_absolute_level() -> None:
    """Signal should be zero when depth is constant (delta = 0)."""
    alpha = MultilevelOfiAlpha()
    bids, asks = _make_depth([1000, 800, 600, 400, 200], [1000, 800, 600, 400, 200])
    alpha.update(bids=bids, asks=asks)
    # Same depth again -> delta = 0 -> raw OFI = 0
    for _ in range(50):
        alpha.update(bids=bids, asks=asks)
    assert abs(alpha.get_signal()) < 0.01


def test_no_cross_instance_leakage() -> None:
    """Two independent instances must not share state."""
    a1 = MultilevelOfiAlpha()
    a2 = MultilevelOfiAlpha()
    bids, asks = _make_depth([500, 400, 300, 200, 100], [100, 80, 60, 40, 20])
    a1.update(bids=bids, asks=asks)
    # a2 has never seen this data
    assert a2.get_signal() == 0.0


def test_prev_state_not_shared_after_copy() -> None:
    """Mutating input arrays after update should not affect stored state."""
    alpha = MultilevelOfiAlpha()
    bids = np.array([[100, 50], [99, 40], [98, 30], [97, 20], [96, 10]], dtype=np.float64)
    asks = np.array([[101, 50], [102, 40], [103, 30], [104, 20], [105, 10]], dtype=np.float64)
    alpha.update(bids=bids, asks=asks)
    sig1 = alpha.get_signal()
    # Mutate input arrays
    bids[:, 1] = 0.0
    asks[:, 1] = 0.0
    # Signal should not change
    assert alpha.get_signal() == sig1
