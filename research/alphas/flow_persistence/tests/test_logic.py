"""Gate B correctness tests for FlowPersistenceAlpha (ref 089)."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.flow_persistence.impl import (
    _A4,
    _A8,
    _A32,
    ALPHA_CLASS,
    FlowPersistenceAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert FlowPersistenceAlpha().manifest.alpha_id == "flow_persistence"


def test_manifest_hypothesis() -> None:
    m = FlowPersistenceAlpha().manifest
    assert "persistence" in m.hypothesis.lower()
    assert "agreement" in m.hypothesis.lower()


def test_manifest_formula() -> None:
    m = FlowPersistenceAlpha().manifest
    assert "EMA_4" in m.formula
    assert "EMA_32" in m.formula
    assert "agreement" in m.formula


def test_manifest_paper_refs_includes_089() -> None:
    assert "089" in FlowPersistenceAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = FlowPersistenceAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_complexity() -> None:
    assert FlowPersistenceAlpha().manifest.complexity == "O(1)"


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D."""
    assert FlowPersistenceAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert FlowPersistenceAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = FlowPersistenceAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert FlowPersistenceAlpha().manifest.tier == AlphaTier.TIER_2


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is FlowPersistenceAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_equal_queues_signal_zero() -> None:
    """Equal bid and ask queues -> qi = 0 -> agreement = 0 -> signal = 0."""
    alpha = FlowPersistenceAlpha()
    sig = alpha.update(100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_persistent_direction_positive_agreement() -> None:
    """Sustained bid dominance -> both EMAs positive -> agreement > 0."""
    alpha = FlowPersistenceAlpha()
    for _ in range(50):
        alpha.update(500.0, 100.0)
    assert alpha.get_signal() > 0.0


def test_persistent_direction_negative_agreement() -> None:
    """Sustained ask dominance -> both EMAs negative -> agreement > 0.

    Note: agreement = fast * slow -> both negative -> product is positive.
    """
    alpha = FlowPersistenceAlpha()
    for _ in range(50):
        alpha.update(100.0, 500.0)
    # Both fast and slow are negative, so agreement = neg * neg = positive
    assert alpha.get_signal() > 0.0


def test_signal_bounded_in_unit_interval() -> None:
    """Signal must stay in [-1, 1] at all times."""
    alpha = FlowPersistenceAlpha()
    rng = np.random.default_rng(42)
    bids = rng.uniform(0, 1000, 200)
    asks = rng.uniform(0, 1000, 200)
    for b, a in zip(bids, asks):
        sig = alpha.update(b, a)
        assert -1.0 <= sig <= 1.0


def test_signal_bounded_extreme_inputs() -> None:
    """Even with extreme inputs, signal stays in [-1, 1]."""
    alpha = FlowPersistenceAlpha()
    for _ in range(200):
        sig = alpha.update(1e9, 0.0)
        assert -1.0 <= sig <= 1.0


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_convergence_constant_input() -> None:
    """Given constant input, EMAs should converge and signal should stabilize."""
    alpha = FlowPersistenceAlpha()
    bid, ask = 300.0, 100.0
    expected_qi = (bid - ask) / (bid + ask)
    # With constant qi, fast->qi, slow->qi, agreement->qi^2, signal->qi^2
    for _ in range(500):
        alpha.update(bid, ask)
    expected_agreement = expected_qi * expected_qi
    assert alpha.get_signal() == pytest.approx(expected_agreement, abs=1e-3)


def test_ema_first_step_values() -> None:
    """After first update, check the internal EMA values are correct."""
    alpha = FlowPersistenceAlpha()
    bid, ask = 400.0, 100.0
    qi = (bid - ask) / (bid + ask)
    alpha.update(bid, ask)

    # After one step: fast = 0 + A4*(qi-0) = A4*qi
    expected_fast = _A4 * qi
    # slow = A32 * qi
    expected_slow = _A32 * qi
    # agreement = fast * slow
    expected_agreement = expected_fast * expected_slow
    # agreement_ema = A8 * agreement
    expected_signal = _A8 * expected_agreement

    assert alpha.get_signal() == pytest.approx(expected_signal, abs=1e-12)


def test_ema_two_step_manual() -> None:
    """Verify two-step EMA computation manually."""
    alpha = FlowPersistenceAlpha()
    b1, a1 = 300.0, 100.0
    b2, a2 = 100.0, 300.0
    qi1 = (b1 - a1) / (b1 + a1)
    qi2 = (b2 - a2) / (b2 + a2)

    alpha.update(b1, a1)
    # Step 1
    fast1 = _A4 * qi1
    slow1 = _A32 * qi1
    agr1 = fast1 * slow1
    agr_ema1 = _A8 * agr1

    sig2 = alpha.update(b2, a2)
    # Step 2
    fast2 = fast1 + _A4 * (qi2 - fast1)
    slow2 = slow1 + _A32 * (qi2 - slow1)
    agr2 = fast2 * slow2
    agr_ema2 = agr_ema1 + _A8 * (agr2 - agr_ema1)

    assert sig2 == pytest.approx(agr_ema2, abs=1e-12)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = FlowPersistenceAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    assert isinstance(sig, float)
    assert sig != 0.0


def test_update_accepts_bids_asks_arrays() -> None:
    alpha = FlowPersistenceAlpha()
    bids = np.array([[100.0, 500.0], [99.0, 300.0]])
    asks = np.array([[101.0, 200.0], [102.0, 150.0]])
    sig = alpha.update(bids=bids, asks=asks)
    assert isinstance(sig, float)


def test_update_one_arg_raises() -> None:
    alpha = FlowPersistenceAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_reset_clears_state() -> None:
    alpha = FlowPersistenceAlpha()
    alpha.update(800.0, 100.0)
    alpha.update(800.0, 100.0)
    alpha.reset()
    bid, ask = 300.0, 300.0
    sig = alpha.update(bid, ask)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_get_signal_before_update_is_zero() -> None:
    alpha = FlowPersistenceAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = FlowPersistenceAlpha()
    assert isinstance(alpha, AlphaProtocol)
