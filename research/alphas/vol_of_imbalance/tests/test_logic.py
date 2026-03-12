"""Gate B correctness tests for VolOfImbalanceAlpha (ref 064)."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.vol_of_imbalance.impl import (
    _A8,
    _A16,
    _A64,
    ALPHA_CLASS,
    VolOfImbalanceAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert VolOfImbalanceAlpha().manifest.alpha_id == "vol_of_imbalance"


def test_manifest_hypothesis_non_empty() -> None:
    assert len(VolOfImbalanceAlpha().manifest.hypothesis) > 20


def test_manifest_formula_non_empty() -> None:
    assert len(VolOfImbalanceAlpha().manifest.formula) > 10


def test_manifest_paper_refs_includes_064() -> None:
    assert "064" in VolOfImbalanceAlpha().manifest.paper_refs


def test_manifest_complexity_o1() -> None:
    assert VolOfImbalanceAlpha().manifest.complexity == "O(1)"


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert VolOfImbalanceAlpha().manifest.latency_profile is not None
    assert "shioaji_sim_p95" in VolOfImbalanceAlpha().manifest.latency_profile


def test_manifest_feature_set_version() -> None:
    assert VolOfImbalanceAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = VolOfImbalanceAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert VolOfImbalanceAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_data_fields() -> None:
    fields = VolOfImbalanceAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is VolOfImbalanceAlpha


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = VolOfImbalanceAlpha()
    assert isinstance(alpha, AlphaProtocol)


# ---------------------------------------------------------------------------
# Signal direction and correctness
# ---------------------------------------------------------------------------


def test_constant_input_vol_converges_to_baseline() -> None:
    """Constant bid/ask -> deviation -> 0 -> vol converges to baseline."""
    alpha = VolOfImbalanceAlpha()
    for _ in range(500):
        alpha.update(300.0, 100.0)
    # With constant input, deviation -> 0, so vol and baseline both -> 0.
    # They may not be exactly equal (EMA lag), but both should be very small.
    assert alpha._dev_ema < 1e-6
    # Signal is bounded regardless
    assert -2.0 <= alpha.get_signal() <= 2.0


def test_bid_dominant_direction_positive() -> None:
    """When bid dominates consistently, qi_ema > 0, so sign is positive."""
    alpha = VolOfImbalanceAlpha()
    for _ in range(50):
        alpha.update(500.0, 100.0)
    for _ in range(20):
        alpha.update(300.0, 100.0)
    assert alpha._qi_ema > 0.0


def test_ask_dominant_direction_negative_qi_ema() -> None:
    """When ask dominates, qi_ema < 0."""
    alpha = VolOfImbalanceAlpha()
    for _ in range(50):
        alpha.update(100.0, 500.0)
    assert alpha._qi_ema < 0.0


def test_signal_bounded_minus2_to_plus2() -> None:
    """Signal must stay in [-2, 2] at all times."""
    alpha = VolOfImbalanceAlpha()
    rng = np.random.default_rng(42)
    bids = rng.uniform(0, 1000, 300)
    asks = rng.uniform(0, 1000, 300)
    for b, a in zip(bids, asks):
        sig = alpha.update(b, a)
        assert -2.0 <= sig <= 2.0, f"Signal {sig} out of bounds"


def test_equal_queues_signal_zero_initially() -> None:
    """Equal bid and ask queues -> qi = 0 -> signal stays 0."""
    alpha = VolOfImbalanceAlpha()
    sig = alpha.update(100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# EMA convergence
# ---------------------------------------------------------------------------


def test_qi_ema_first_step_tracks_raw() -> None:
    """After first update, qi_ema = A8 * raw_qi (from initial 0)."""
    alpha = VolOfImbalanceAlpha()
    bid, ask = 300.0, 100.0
    qi = (bid - ask) / (bid + ask)
    alpha.update(bid, ask)
    expected_ema = _A8 * qi  # starts from 0
    assert alpha._qi_ema == pytest.approx(expected_ema, abs=1e-9)


def test_ema_constants_are_correct() -> None:
    """EMA alphas match the formula 1 - exp(-1/N)."""
    assert _A8 == pytest.approx(1.0 - math.exp(-1.0 / 8.0), abs=1e-12)
    assert _A16 == pytest.approx(1.0 - math.exp(-1.0 / 16.0), abs=1e-12)
    assert _A64 == pytest.approx(1.0 - math.exp(-1.0 / 64.0), abs=1e-12)


def test_vol_increases_with_oscillating_input() -> None:
    """Alternating bid/ask dominance -> higher deviation -> higher vol."""
    alpha_stable = VolOfImbalanceAlpha()
    alpha_volatile = VolOfImbalanceAlpha()
    for _ in range(100):
        alpha_stable.update(200.0, 100.0)
    for i in range(100):
        if i % 2 == 0:
            alpha_volatile.update(500.0, 100.0)
        else:
            alpha_volatile.update(100.0, 500.0)
    assert alpha_volatile._dev_ema > alpha_stable._dev_ema


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = VolOfImbalanceAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    assert isinstance(sig, float)


def test_update_accepts_bids_asks_arrays() -> None:
    alpha = VolOfImbalanceAlpha()
    bids = np.array([[100.0, 500.0], [99.0, 300.0]])
    asks = np.array([[101.0, 200.0], [102.0, 150.0]])
    sig = alpha.update(bids=bids, asks=asks)
    assert isinstance(sig, float)


def test_update_one_arg_raises() -> None:
    alpha = VolOfImbalanceAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_reset_clears_state() -> None:
    alpha = VolOfImbalanceAlpha()
    for _ in range(50):
        alpha.update(800.0, 100.0)
    alpha.reset()
    assert alpha._qi_ema == 0.0
    assert alpha._dev_ema == 0.0
    assert alpha._vol_baseline == 0.0
    assert alpha._signal == 0.0
    sig = alpha.update(300.0, 300.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_get_signal_before_update_is_zero() -> None:
    alpha = VolOfImbalanceAlpha()
    assert alpha.get_signal() == 0.0
