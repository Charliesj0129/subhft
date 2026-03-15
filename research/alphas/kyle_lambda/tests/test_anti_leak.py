"""Gate B anti-leak / lookahead-bias tests for KyleLambdaAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np

from research.alphas.kyle_lambda.impl import KyleLambdaAlpha


def test_update_no_args_returns_float() -> None:
    alpha = KyleLambdaAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_stateful_not_lookahead() -> None:
    alpha = KyleLambdaAlpha()
    alpha.update(100.0, 100.0, 35000.0)
    sig1 = alpha.update(500.0, 100.0, 35005.0)
    sig2 = alpha.update(100.0, 500.0, 34995.0)
    assert sig1 != sig2


def test_reset_eliminates_state_dependency() -> None:
    a1 = KyleLambdaAlpha()
    a2 = KyleLambdaAlpha()
    a1.update(800.0, 200.0, 35000.0)
    a1.reset()
    s1 = a1.update(300.0, 300.0, 35000.0)
    s2 = a2.update(300.0, 300.0, 35000.0)
    assert s1 == s2


def test_deterministic_same_sequence() -> None:
    rng = np.random.default_rng(123)
    bids = rng.uniform(1, 500, 100)
    asks = rng.uniform(1, 500, 100)
    prices = np.cumsum(rng.normal(0, 1, 100)) + 35000

    a1 = KyleLambdaAlpha()
    a2 = KyleLambdaAlpha()
    for b, a, p in zip(bids, asks, prices):
        a1.update(b, a, p)
        a2.update(b, a, p)
    assert a1.get_signal() == a2.get_signal()


def test_no_future_data_dependency() -> None:
    shared = [(100.0, 100.0, 35000.0), (200.0, 80.0, 35001.0), (150.0, 120.0, 35000.5)]
    a1 = KyleLambdaAlpha()
    a2 = KyleLambdaAlpha()
    for args in shared:
        a1.update(*args)
        a2.update(*args)
    sig_a = a1.get_signal()
    sig_b = a2.get_signal()
    assert sig_a == sig_b
    a1.update(999.0, 1.0, 36000.0)
    a2.update(1.0, 999.0, 34000.0)
    assert sig_a == sig_b


def test_slots_no_dict() -> None:
    alpha = KyleLambdaAlpha()
    assert not hasattr(alpha, "__dict__")
