"""Gate B — anti-leak / lookahead-bias tests for depth_concentration_index."""
from __future__ import annotations

import numpy as np

from research.alphas.depth_concentration_index.impl import (
    DepthConcentrationIndexAlpha,
)


def _make_book(bid_qtys: list[float], ask_qtys: list[float]) -> dict:
    n_bid = len(bid_qtys)
    n_ask = len(ask_qtys)
    bids = np.zeros((n_bid, 2))
    asks = np.zeros((n_ask, 2))
    for i, q in enumerate(bid_qtys):
        bids[i, 0] = 100.0 - i * 0.5
        bids[i, 1] = q
    for i, q in enumerate(ask_qtys):
        asks[i, 0] = 100.5 + i * 0.5
        asks[i, 1] = q
    return {"bids": bids, "asks": asks}


def test_update_is_stateful_not_lookahead() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = DepthConcentrationIndexAlpha()

    # Concentrated asks => positive
    book1 = _make_book([20, 20, 20, 20, 20], [100, 0, 0, 0, 0])
    sig1 = alpha.update(**book1)

    # Flip: concentrated bids => should move toward negative
    book2 = _make_book([100, 0, 0, 0, 0], [20, 20, 20, 20, 20])
    sig2 = alpha.update(**book2)

    assert sig1 > 0.0
    assert sig2 < sig1  # signal moved in correct direction


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed same sequence return identical signals."""
    a1 = DepthConcentrationIndexAlpha()
    a2 = DepthConcentrationIndexAlpha()

    # Warm up a1
    book_warm = _make_book([100, 0, 0, 0, 0], [20, 20, 20, 20, 20])
    a1.update(**book_warm)
    a1.update(**book_warm)
    a1.reset()

    # Same input to both
    book = _make_book([50, 40, 30, 20, 10], [50, 40, 30, 20, 10])
    s1 = a1.update(**book)
    s2 = a2.update(**book)
    assert s1 == s2


def test_order_sensitivity() -> None:
    """Different input orderings produce different signals (no aggregation bug)."""
    a1 = DepthConcentrationIndexAlpha()
    a2 = DepthConcentrationIndexAlpha()

    book_a = _make_book([20, 20, 20, 20, 20], [100, 0, 0, 0, 0])
    book_b = _make_book([100, 0, 0, 0, 0], [20, 20, 20, 20, 20])

    # a1: book_a then book_b
    a1.update(**book_a)
    s1 = a1.update(**book_b)

    # a2: book_b then book_a
    a2.update(**book_b)
    s2 = a2.update(**book_a)

    assert s1 != s2  # order matters due to EMA


def test_no_global_state_leakage() -> None:
    """Two independent instances do not share state."""
    a1 = DepthConcentrationIndexAlpha()
    a2 = DepthConcentrationIndexAlpha()

    book = _make_book([20, 20, 20, 20, 20], [100, 0, 0, 0, 0])
    a1.update(**book)

    # a2 should be unaffected
    assert a2.get_signal() == 0.0
    assert not a2._initialized


def test_deterministic_replay() -> None:
    """Same input sequence always produces same output."""
    results_1: list[float] = []
    results_2: list[float] = []

    books = [
        _make_book([100, 80, 60, 40, 20], [20, 40, 60, 80, 100]),
        _make_book([50, 50, 50, 50, 50], [90, 10, 0, 0, 0]),
        _make_book([30, 30, 30, 30, 30], [30, 30, 30, 30, 30]),
    ]

    for run_results in (results_1, results_2):
        alpha = DepthConcentrationIndexAlpha()
        for book in books:
            run_results.append(alpha.update(**book))

    assert results_1 == results_2
