"""Gate B correctness tests for CrossEmaQiAlpha (ref 127)."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.cross_ema_qi.impl import (
    _A4,
    _A16,
    ALPHA_CLASS,
    CrossEmaQiAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert CrossEmaQiAlpha().manifest.alpha_id == "cross_ema_qi"


def test_manifest_hypothesis() -> None:
    m = CrossEmaQiAlpha().manifest
    assert "EMA crossover" in m.hypothesis


def test_manifest_formula() -> None:
    m = CrossEmaQiAlpha().manifest
    assert "EMA_4" in m.formula
    assert "EMA_16" in m.formula
    assert "clip" in m.formula


def test_manifest_paper_refs_includes_127() -> None:
    assert "127" in CrossEmaQiAlpha().manifest.paper_refs


def test_manifest_complexity() -> None:
    assert CrossEmaQiAlpha().manifest.complexity == "O(1)"


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert CrossEmaQiAlpha().manifest.latency_profile is not None
    assert "shioaji_sim_p95" in CrossEmaQiAlpha().manifest.latency_profile


def test_manifest_feature_set_version() -> None:
    assert CrossEmaQiAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = CrossEmaQiAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_manifest_data_fields() -> None:
    fields = CrossEmaQiAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert CrossEmaQiAlpha().manifest.tier == AlphaTier.TIER_2


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is CrossEmaQiAlpha


# ---------------------------------------------------------------------------
# Signal direction and boundary conditions
# ---------------------------------------------------------------------------


def test_equal_queues_signal_zero() -> None:
    """Equal bid and ask queues consistently -> signal = 0."""
    alpha = CrossEmaQiAlpha()
    for _ in range(50):
        sig = alpha.update(100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_bid_dominant_signal_positive() -> None:
    """Sustained bid dominance -> fast EMA rises faster -> positive crossover."""
    alpha = CrossEmaQiAlpha()
    # Start from neutral, then shift to bid-dominant
    for _ in range(10):
        alpha.update(100.0, 100.0)
    for _ in range(20):
        sig = alpha.update(500.0, 100.0)
    assert sig > 0.0


def test_ask_dominant_signal_negative() -> None:
    """Sustained ask dominance -> fast EMA drops faster -> negative crossover."""
    alpha = CrossEmaQiAlpha()
    for _ in range(10):
        alpha.update(100.0, 100.0)
    for _ in range(20):
        sig = alpha.update(100.0, 500.0)
    assert sig < 0.0


def test_signal_bounded_minus_one_to_one() -> None:
    """Signal must stay in [-1, 1] at all times."""
    alpha = CrossEmaQiAlpha()
    rng = np.random.default_rng(42)
    bids = rng.uniform(0, 1000, 300)
    asks = rng.uniform(0, 1000, 300)
    for b, a in zip(bids, asks):
        sig = alpha.update(b, a)
        assert -1.0 <= sig <= 1.0


def test_signal_direction_momentum_shift() -> None:
    """Shift from ask-dominant to bid-dominant should yield positive signal."""
    alpha = CrossEmaQiAlpha()
    # Build ask pressure
    for _ in range(30):
        alpha.update(50.0, 500.0)
    # Now shift to bid pressure
    for _ in range(10):
        sig = alpha.update(500.0, 50.0)
    # Fast EMA should be above slow EMA after momentum shift
    assert sig > 0.0


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_constants() -> None:
    """Verify EMA constants match formula."""
    assert _A4 == pytest.approx(1.0 - math.exp(-1.0 / 4.0), abs=1e-12)
    assert _A16 == pytest.approx(1.0 - math.exp(-1.0 / 16.0), abs=1e-12)


def test_ema_convergence_constant_input() -> None:
    """With constant input, fast and slow EMAs converge -> signal approaches 0."""
    alpha = CrossEmaQiAlpha()
    for _ in range(500):
        alpha.update(300.0, 100.0)
    # Both EMAs converge to the same QI -> difference -> 0
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-3)


def test_first_tick_signal_consistent() -> None:
    """First update: both EMAs start at 0, so signal = A4*qi - A16*qi = (A4-A16)*qi."""
    alpha = CrossEmaQiAlpha()
    bid, ask = 400.0, 100.0
    qi = (bid - ask) / (bid + ask)
    expected = (_A4 - _A16) * qi
    sig = alpha.update(bid, ask)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_second_tick_ema_update() -> None:
    """Verify EMA update formula on second tick."""
    alpha = CrossEmaQiAlpha()
    b1, a1 = 300.0, 100.0
    b2, a2 = 100.0, 300.0
    qi1 = (b1 - a1) / (b1 + a1)
    qi2 = (b2 - a2) / (b2 + a2)

    alpha.update(b1, a1)
    sig2 = alpha.update(b2, a2)

    fast_after_1 = _A4 * qi1
    slow_after_1 = _A16 * qi1
    fast_after_2 = fast_after_1 + _A4 * (qi2 - fast_after_1)
    slow_after_2 = slow_after_1 + _A16 * (qi2 - slow_after_1)
    expected = fast_after_2 - slow_after_2
    assert sig2 == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_positional_args() -> None:
    alpha = CrossEmaQiAlpha()
    sig = alpha.update(200.0, 100.0)
    assert isinstance(sig, float)


def test_update_accepts_keyword_args() -> None:
    alpha = CrossEmaQiAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    alpha2 = CrossEmaQiAlpha()
    sig2 = alpha2.update(200.0, 100.0)
    assert sig == pytest.approx(sig2, abs=1e-12)


def test_update_accepts_bids_asks_arrays() -> None:
    alpha = CrossEmaQiAlpha()
    bids = np.array([[100.0, 500.0], [99.0, 300.0]])
    asks = np.array([[101.0, 200.0], [102.0, 150.0]])
    sig = alpha.update(bids=bids, asks=asks)
    alpha2 = CrossEmaQiAlpha()
    sig2 = alpha2.update(500.0, 200.0)
    assert sig == pytest.approx(sig2, abs=1e-12)


def test_update_one_arg_raises() -> None:
    alpha = CrossEmaQiAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_get_signal_returns_last() -> None:
    alpha = CrossEmaQiAlpha()
    ret = alpha.update(300.0, 100.0)
    assert alpha.get_signal() == ret


def test_get_signal_before_update_is_zero() -> None:
    alpha = CrossEmaQiAlpha()
    assert alpha.get_signal() == 0.0


def test_reset_clears_state() -> None:
    alpha = CrossEmaQiAlpha()
    alpha.update(800.0, 100.0)
    alpha.update(800.0, 100.0)
    alpha.reset()
    # After reset, get_signal should be 0
    assert alpha.get_signal() == 0.0
    # First update after reset should match fresh alpha
    fresh = CrossEmaQiAlpha()
    sig1 = alpha.update(300.0, 300.0)
    sig2 = fresh.update(300.0, 300.0)
    assert sig1 == pytest.approx(sig2, abs=1e-12)


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = CrossEmaQiAlpha()
    assert isinstance(alpha, AlphaProtocol)
