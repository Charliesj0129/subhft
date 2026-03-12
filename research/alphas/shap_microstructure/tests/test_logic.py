"""Gate B correctness tests for ShapMicrostructureAlpha (ref 082)."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.shap_microstructure.impl import (
    ALPHA_CLASS,
    ShapMicrostructureAlpha,
    _EMA_ALPHA,
    _W_IMBALANCE,
    _W_SPREAD_CHANGE,
    _W_VOL_SURPRISE,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert ShapMicrostructureAlpha().manifest.alpha_id == "shap_microstructure"


def test_manifest_tier_is_ensemble() -> None:
    from research.registry.schemas import AlphaTier

    assert ShapMicrostructureAlpha().manifest.tier == AlphaTier.ENSEMBLE


def test_manifest_paper_refs_includes_082() -> None:
    assert "082" in ShapMicrostructureAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = ShapMicrostructureAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert ShapMicrostructureAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert ShapMicrostructureAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = ShapMicrostructureAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is ShapMicrostructureAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_equal_queues_first_tick_signal_zero() -> None:
    """Equal bid and ask → imbalance=0, spread_proxy=0, vol_surprise=0 → signal=0."""
    alpha = ShapMicrostructureAlpha()
    sig = alpha.update(100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_signal_bounded_minus2_to_2() -> None:
    """Signal must stay in [-2, 2] under random inputs."""
    alpha = ShapMicrostructureAlpha()
    rng = np.random.default_rng(42)
    bids = rng.uniform(0, 1000, 500)
    asks = rng.uniform(0, 1000, 500)
    for b, a in zip(bids, asks):
        sig = alpha.update(b, a)
        assert -2.0 <= sig <= 2.0, f"Signal {sig} out of bounds"


def test_bid_dominant_signal_positive() -> None:
    """Sustained bid dominance → positive signal."""
    alpha = ShapMicrostructureAlpha()
    for _ in range(50):
        alpha.update(500.0, 100.0)
    assert alpha.get_signal() > 0.0


def test_ask_dominant_signal_negative() -> None:
    """Sustained ask dominance → negative signal."""
    alpha = ShapMicrostructureAlpha()
    for _ in range(50):
        alpha.update(100.0, 500.0)
    assert alpha.get_signal() < 0.0


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_converges_constant_input() -> None:
    """With constant bid/ask, signal converges to steady-state composite."""
    alpha = ShapMicrostructureAlpha()
    bid, ask = 300.0, 100.0

    # After many ticks, spread_change → 0, vol_surprise → 0
    # Only imbalance persists: (300-100)/(300+100) = 0.5
    # composite → 0.45 * 0.5 + 0.30 * 0 + 0.25 * 0 = 0.225
    for _ in range(200):
        alpha.update(bid, ask)

    expected_imbalance = (bid - ask) / (bid + ask)
    expected = _W_IMBALANCE * expected_imbalance
    assert alpha.get_signal() == pytest.approx(expected, abs=1e-3)


def test_first_tick_initializes_ema() -> None:
    """First update initializes EMA to composite (no prior history)."""
    alpha = ShapMicrostructureAlpha()
    bid, ask = 400.0, 100.0
    sig = alpha.update(bid, ask)
    # tick_count=0 at entry → vol_surprise=0, spread_change = spread_proxy - 0
    total = bid + ask
    imbalance = (bid - ask) / total
    spread_proxy = (ask - bid) / total
    composite = _W_IMBALANCE * imbalance + _W_SPREAD_CHANGE * spread_proxy
    assert sig == pytest.approx(composite, abs=1e-9)


def test_ema_second_step_formula() -> None:
    """Second step: EMA = prev + alpha*(new_composite - prev)."""
    alpha = ShapMicrostructureAlpha()
    b1, a1 = 300.0, 100.0
    b2, a2 = 100.0, 300.0

    # Tick 1
    t1 = b1 + a1
    imb1 = (b1 - a1) / t1
    sp1 = (a1 - b1) / t1
    comp1 = _W_IMBALANCE * imb1 + _W_SPREAD_CHANGE * (sp1 - 0.0)
    # vol_surprise = 0 for first tick
    sig1 = alpha.update(b1, a1)
    assert sig1 == pytest.approx(comp1, abs=1e-9)

    # Tick 2
    t2 = b2 + a2
    imb2 = (b2 - a2) / t2
    sp2 = (a2 - b2) / t2
    sc2 = sp2 - sp1
    vs2 = (t2 - t1) / (t1 + 1e-8)
    comp2 = _W_IMBALANCE * imb2 + _W_SPREAD_CHANGE * sc2 + _W_VOL_SURPRISE * vs2
    expected = comp1 + _EMA_ALPHA * (comp2 - comp1)
    sig2 = alpha.update(b2, a2)
    assert sig2 == pytest.approx(expected, abs=1e-9)


def test_weights_sum_to_one() -> None:
    """SHAP weights must sum to 1.0."""
    assert _W_IMBALANCE + _W_SPREAD_CHANGE + _W_VOL_SURPRISE == pytest.approx(
        1.0, abs=1e-9
    )


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = ShapMicrostructureAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    total = 300.0
    imb = 100.0 / total
    sp = -100.0 / total
    expected = _W_IMBALANCE * imb + _W_SPREAD_CHANGE * sp
    assert sig == pytest.approx(expected, abs=1e-9)


def test_update_accepts_bids_asks_arrays() -> None:
    alpha = ShapMicrostructureAlpha()
    bids = np.array([[100.0, 500.0], [99.0, 300.0]])
    asks = np.array([[101.0, 200.0], [102.0, 150.0]])
    sig = alpha.update(bids=bids, asks=asks)
    # bid_qty=500, ask_qty=200
    total = 700.0
    imb = 300.0 / total
    sp = -300.0 / total
    expected = _W_IMBALANCE * imb + _W_SPREAD_CHANGE * sp
    assert sig == pytest.approx(expected, abs=1e-9)


def test_update_one_arg_raises() -> None:
    alpha = ShapMicrostructureAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_reset_clears_state() -> None:
    alpha = ShapMicrostructureAlpha()
    alpha.update(800.0, 100.0)
    alpha.update(200.0, 300.0)
    alpha.reset()
    # After reset, behaves like fresh instance
    sig = alpha.update(300.0, 300.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_get_signal_before_update_is_zero() -> None:
    alpha = ShapMicrostructureAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = ShapMicrostructureAlpha()
    assert isinstance(alpha, AlphaProtocol)


# ---------------------------------------------------------------------------
# Feature-level correctness
# ---------------------------------------------------------------------------


def test_vol_surprise_detects_depth_spike() -> None:
    """A sudden depth increase should shift signal via vol_surprise component."""
    alpha = ShapMicrostructureAlpha()
    # Baseline: stable depth
    for _ in range(20):
        alpha.update(100.0, 100.0)
    sig_before = alpha.get_signal()

    # Spike: 10x depth
    sig_after = alpha.update(1000.0, 1000.0)
    # vol_surprise = (2000 - 200) / 200 = 9.0 → large positive composite shift
    assert sig_after > sig_before


def test_spread_change_component() -> None:
    """Widening spread proxy (more ask, less bid) creates negative composite shift."""
    alpha = ShapMicrostructureAlpha()
    # Tick 1: balanced
    alpha.update(500.0, 500.0)
    # Tick 2: ask expands → spread proxy increases → spread_change > 0
    # But imbalance goes negative. Net effect depends on weights.
    sig = alpha.update(200.0, 800.0)
    # imbalance = (200-800)/1000 = -0.6 (negative, large)
    # Overall should be negative due to dominant imbalance weight
    assert sig < 0.0
