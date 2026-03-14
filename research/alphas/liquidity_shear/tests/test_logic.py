"""Gate B correctness tests for LiquidityShearAlpha (ref 032)."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.liquidity_shear.impl import (
    _EMA_ALPHA,
    ALPHA_CLASS,
    LiquidityShearAlpha,
    _depth_slope,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert LiquidityShearAlpha().manifest.alpha_id == "liquidity_shear"


def test_manifest_tier_is_ensemble() -> None:
    from research.registry.schemas import AlphaTier

    assert LiquidityShearAlpha().manifest.tier == AlphaTier.ENSEMBLE


def test_manifest_paper_refs_includes_032() -> None:
    assert "032" in LiquidityShearAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = LiquidityShearAlpha().manifest.data_fields
    assert "bids" in fields
    assert "asks" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert LiquidityShearAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert LiquidityShearAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = LiquidityShearAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is LiquidityShearAlpha


# ---------------------------------------------------------------------------
# depth_slope helper
# ---------------------------------------------------------------------------


def test_depth_slope_empty_array() -> None:
    """Empty book side returns 0."""
    result = _depth_slope(np.empty((0, 2)))
    assert result == 0.0


def test_depth_slope_single_level() -> None:
    """Single level: slope = qty / (1 * qty) = 1.0."""
    book = np.array([[100.0, 50.0]])
    assert _depth_slope(book) == pytest.approx(1.0, abs=1e-9)


def test_depth_slope_uniform_depth() -> None:
    """Uniform qty across levels: slope = N*q / (q * sum(1..N))."""
    n = 5
    qty = 100.0
    book = np.array([[100.0 - i, qty] for i in range(n)])
    # total = n * qty, weighted = qty * (1 + 2 + ... + n) = qty * n*(n+1)/2
    expected = (n * qty) / (qty * n * (n + 1) / 2)
    assert _depth_slope(book) == pytest.approx(expected, abs=1e-9)


def test_depth_slope_concentrated_at_bbo() -> None:
    """Most qty at level 1 → high slope (steep decay)."""
    book = np.array(
        [
            [100.0, 1000.0],  # level 1: huge qty
            [99.0, 1.0],  # level 2: tiny
            [98.0, 1.0],  # level 3: tiny
        ]
    )
    slope = _depth_slope(book)
    assert slope > 0.9  # concentrated near top


def test_depth_slope_concentrated_far_from_bbo() -> None:
    """Most qty at far level → low slope (flat/inverted decay)."""
    book = np.array(
        [
            [100.0, 1.0],  # level 1: tiny
            [99.0, 1.0],  # level 2: tiny
            [98.0, 1000.0],  # level 3: huge qty
        ]
    )
    slope = _depth_slope(book)
    assert slope < 0.4  # weight far from BBO


def test_depth_slope_zero_qty() -> None:
    """All zero quantities returns 0."""
    book = np.array([[100.0, 0.0], [99.0, 0.0]])
    assert _depth_slope(book) == 0.0


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_symmetric_book_signal_zero() -> None:
    """Symmetric bid/ask books → shear = 0, signal = 0."""
    alpha = LiquidityShearAlpha()
    bids = np.array([[100.0, 50.0], [99.0, 30.0], [98.0, 10.0]])
    asks = np.array([[101.0, 50.0], [102.0, 30.0], [103.0, 10.0]])
    sig = alpha.update(bids=bids, asks=asks)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_steeper_ask_signal_positive() -> None:
    """Ask side concentrated at BBO (steeper) → positive shear → buying pressure."""
    alpha = LiquidityShearAlpha()
    bids = np.array([[100.0, 30.0], [99.0, 30.0], [98.0, 30.0]])  # flat
    asks = np.array([[101.0, 90.0], [102.0, 5.0], [103.0, 5.0]])  # concentrated
    for _ in range(20):
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() > 0.0


def test_steeper_bid_signal_negative() -> None:
    """Bid side concentrated at BBO (steeper) → negative shear → selling pressure."""
    alpha = LiquidityShearAlpha()
    bids = np.array([[100.0, 90.0], [99.0, 5.0], [98.0, 5.0]])  # concentrated
    asks = np.array([[101.0, 30.0], [102.0, 30.0], [103.0, 30.0]])  # flat
    for _ in range(20):
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() < 0.0


def test_signal_bounded_in_clip_range() -> None:
    """Signal must stay in [-2, 2] under extreme inputs."""
    alpha = LiquidityShearAlpha()
    # Extreme asymmetry: huge ask slope, tiny bid slope
    bids_extreme = np.array([[100.0, 1.0], [99.0, 1.0], [98.0, 1000.0]])
    asks_extreme = np.array([[101.0, 1000.0], [102.0, 1.0], [103.0, 1.0]])
    for _ in range(200):
        sig = alpha.update(bids=bids_extreme, asks=asks_extreme)
        assert -2.0 <= sig <= 2.0


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_converges_constant_input() -> None:
    """EMA should converge to the raw shear given constant input."""
    alpha = LiquidityShearAlpha()
    bids = np.array([[100.0, 80.0], [99.0, 20.0]])
    asks = np.array([[101.0, 40.0], [102.0, 60.0]])
    bid_slope = _depth_slope(bids)
    ask_slope = _depth_slope(asks)
    expected_shear = math.log(ask_slope / bid_slope)
    for _ in range(200):
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() == pytest.approx(expected_shear, abs=1e-4)


def test_ema_first_step_initializes_to_raw_shear() -> None:
    """First update initializes EMA to the raw shear (no prior history)."""
    alpha = LiquidityShearAlpha()
    bids = np.array([[100.0, 80.0], [99.0, 20.0]])
    asks = np.array([[101.0, 40.0], [102.0, 60.0]])
    bid_slope = _depth_slope(bids)
    ask_slope = _depth_slope(asks)
    expected = math.log(ask_slope / bid_slope)
    sig = alpha.update(bids=bids, asks=asks)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_ema_decay_second_step() -> None:
    """Second step: EMA = prev + alpha*(raw - prev)."""
    alpha = LiquidityShearAlpha()
    bids1 = np.array([[100.0, 80.0], [99.0, 20.0]])
    asks1 = np.array([[101.0, 40.0], [102.0, 60.0]])
    bids2 = np.array([[100.0, 40.0], [99.0, 60.0]])
    asks2 = np.array([[101.0, 80.0], [102.0, 20.0]])

    s1_bid = _depth_slope(bids1)
    s1_ask = _depth_slope(asks1)
    shear1 = math.log(s1_ask / s1_bid)

    s2_bid = _depth_slope(bids2)
    s2_ask = _depth_slope(asks2)
    shear2 = math.log(s2_ask / s2_bid)

    expected = shear1 + _EMA_ALPHA * (shear2 - shear1)
    alpha.update(bids=bids1, asks=asks1)
    sig2 = alpha.update(bids=bids2, asks=asks2)
    assert sig2 == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_no_depth_returns_neutral() -> None:
    """Without bids/asks arrays, signal stays at 0 (neutral)."""
    alpha = LiquidityShearAlpha()
    sig = alpha.update()
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_update_with_bid_ask_qty_only_neutral() -> None:
    """Scalar bid_qty/ask_qty without depth profile → neutral."""
    alpha = LiquidityShearAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_reset_clears_state() -> None:
    alpha = LiquidityShearAlpha()
    bids = np.array([[100.0, 90.0], [99.0, 5.0], [98.0, 5.0]])
    asks = np.array([[101.0, 30.0], [102.0, 30.0], [103.0, 30.0]])
    alpha.update(bids=bids, asks=asks)
    alpha.reset()
    # After reset, symmetric book → signal 0
    sym = np.array([[100.0, 50.0], [99.0, 50.0]])
    sig = alpha.update(bids=sym, asks=sym)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_get_signal_before_update_is_zero() -> None:
    alpha = LiquidityShearAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = LiquidityShearAlpha()
    assert isinstance(alpha, AlphaProtocol)
