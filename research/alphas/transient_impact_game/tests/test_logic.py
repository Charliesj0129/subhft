"""Gate B correctness tests for TransientImpactGameAlpha (ref 013)."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.transient_impact_game.impl import (
    _DECAY_RATE,
    _EMA_ALPHA,
    _EPSILON,
    ALPHA_CLASS,
    TransientImpactGameAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert TransientImpactGameAlpha().manifest.alpha_id == "transient_impact_game"


def test_manifest_tier_is_ensemble() -> None:
    from research.registry.schemas import AlphaTier

    assert TransientImpactGameAlpha().manifest.tier == AlphaTier.ENSEMBLE


def test_manifest_paper_refs_includes_013() -> None:
    assert "013" in TransientImpactGameAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = TransientImpactGameAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert TransientImpactGameAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert TransientImpactGameAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = TransientImpactGameAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is TransientImpactGameAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_first_tick_signal_zero() -> None:
    """First update initializes state; signal should be 0."""
    alpha = TransientImpactGameAlpha()
    sig = alpha.update(100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_constant_input_signal_decays_to_zero() -> None:
    """With constant input (no OFI changes), transient impact decays to 0.

    After the first tick, OFI = 0 each tick, so transient impact decays
    and signal should approach 0.
    """
    alpha = TransientImpactGameAlpha()
    alpha.update(100.0, 100.0)  # init
    for _ in range(200):
        sig = alpha.update(100.0, 100.0)
    assert abs(sig) < 0.01


def test_bid_increase_produces_negative_signal() -> None:
    """Sustained bid increase -> positive OFI -> transient impact grows.

    Signal = -transient / total, so it should be negative.
    """
    alpha = TransientImpactGameAlpha()
    alpha.update(100.0, 100.0)
    for i in range(50):
        sig = alpha.update(100.0 + (i + 1) * 10.0, 100.0)
    assert sig < 0.0


def test_signal_returns_float() -> None:
    alpha = TransientImpactGameAlpha()
    sig = alpha.update(100.0, 100.0)
    assert isinstance(sig, float)


def test_signal_finite_under_random_input() -> None:
    """Signal must remain finite under random input."""
    alpha = TransientImpactGameAlpha()
    rng = np.random.default_rng(42)
    bids = rng.uniform(1, 1000, 500)
    asks = rng.uniform(1, 1000, 500)
    for b, a in zip(bids, asks):
        sig = alpha.update(b, a)
        assert math.isfinite(sig)


# ---------------------------------------------------------------------------
# EMA and decay mechanics
# ---------------------------------------------------------------------------


def test_transient_impact_decays_with_no_flow() -> None:
    """After a burst of flow followed by constant input, internal transient decays."""
    alpha = TransientImpactGameAlpha()
    # Init
    alpha.update(100.0, 100.0)
    # Burst: large bid increase
    alpha.update(500.0, 100.0)
    transient_after_burst = alpha._transient_impact

    # Now hold constant for many ticks (OFI = 0)
    for _ in range(200):
        alpha.update(500.0, 100.0)
    transient_after_decay = alpha._transient_impact

    # Internal transient impact state should decay toward 0
    assert transient_after_decay < transient_after_burst * 0.01


def test_second_tick_ema_step() -> None:
    """Verify the EMA step at tick 2 matches the formula (with clipping)."""
    alpha = TransientImpactGameAlpha()
    # tick 0: init (signal=0)
    alpha.update(100.0, 100.0)
    # tick 1: OFI = (200 - 100) - (100 - 100) = 100
    sig = alpha.update(200.0, 100.0)

    ofi = 100.0
    abs_ofi = abs(ofi)
    transient = 0.0 * (1.0 - _DECAY_RATE) + abs_ofi  # 100.0
    total_ema = 0.0 + _EMA_ALPHA * (abs_ofi - 0.0)  # _EMA_ALPHA * 100
    ratio = -transient / (total_ema + _EPSILON)
    # Ratio is clipped to [-1, 0]
    ratio = max(-1.0, min(0.0, ratio))
    expected_signal = 0.0 + _EMA_ALPHA * (ratio - 0.0)

    assert sig == pytest.approx(expected_signal, abs=1e-9)


def test_decay_rate_applied_correctly() -> None:
    """Transient impact at tick 2 = prev * (1-decay) + new_abs_ofi."""
    alpha = TransientImpactGameAlpha()
    alpha.update(100.0, 100.0)
    # tick 1: OFI = 100
    alpha.update(200.0, 100.0)
    # tick 2: OFI = (200 - 200) - (100 - 100) = 0
    alpha.update(200.0, 100.0)

    # transient after tick 1: 100.0
    # transient after tick 2: 100.0 * (1 - 0.05) + 0 = 95.0
    expected_transient = 100.0 * (1.0 - _DECAY_RATE)
    assert alpha._transient_impact == pytest.approx(expected_transient, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = TransientImpactGameAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    assert isinstance(sig, float)


def test_update_accepts_bids_asks_arrays() -> None:
    alpha = TransientImpactGameAlpha()
    bids = np.array([[100.0, 500.0], [99.0, 300.0]])
    asks = np.array([[101.0, 200.0], [102.0, 150.0]])
    sig = alpha.update(bids=bids, asks=asks)
    assert isinstance(sig, float)


def test_update_one_arg_raises() -> None:
    alpha = TransientImpactGameAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_reset_clears_state() -> None:
    alpha = TransientImpactGameAlpha()
    alpha.update(800.0, 100.0)
    alpha.update(100.0, 800.0)
    alpha.reset()
    # After reset, first update should return 0 (init tick)
    sig = alpha.update(300.0, 300.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_get_signal_before_update_is_zero() -> None:
    alpha = TransientImpactGameAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = TransientImpactGameAlpha()
    assert isinstance(alpha, AlphaProtocol)


def test_get_signal_matches_update_return() -> None:
    """get_signal() should return the same value as the last update()."""
    alpha = TransientImpactGameAlpha()
    alpha.update(100.0, 100.0)
    sig = alpha.update(200.0, 50.0)
    assert alpha.get_signal() == sig


def test_reset_then_identical_sequence_matches_fresh() -> None:
    """After reset, feeding the same sequence gives identical signals."""
    seq = [(100.0, 100.0), (200.0, 80.0), (150.0, 120.0), (180.0, 90.0)]

    a1 = TransientImpactGameAlpha()
    a1.update(999.0, 1.0)
    a1.update(1.0, 999.0)
    a1.reset()
    for b, a in seq:
        a1.update(b, a)

    a2 = TransientImpactGameAlpha()
    for b, a in seq:
        a2.update(b, a)

    assert a1.get_signal() == pytest.approx(a2.get_signal(), abs=1e-12)
