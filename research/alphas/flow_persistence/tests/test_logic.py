"""Gate B correctness tests for FlowPersistenceAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.flow_persistence.impl import (
    _EMA_ALPHA_8,
    _EMA_ALPHA_16,
    _EPSILON,
    ALPHA_CLASS,
    FlowPersistenceAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert FlowPersistenceAlpha().manifest.alpha_id == "flow_persistence"


def test_manifest_data_fields() -> None:
    fields = FlowPersistenceAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_complexity() -> None:
    assert FlowPersistenceAlpha().manifest.complexity == "O(1)"


def test_manifest_latency_profile_set() -> None:
    assert FlowPersistenceAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert FlowPersistenceAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert FlowPersistenceAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = FlowPersistenceAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is FlowPersistenceAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_equal_queues_signal_zero() -> None:
    """Equal bid and ask queues -> OFI_raw = 0 -> signal = 0."""
    alpha = FlowPersistenceAlpha()
    sig = alpha.update(100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_persistent_bid_dominance_positive() -> None:
    """Sustained bid dominance -> ema_ofi > 0 -> signal > 0."""
    alpha = FlowPersistenceAlpha()
    for _ in range(50):
        alpha.update(500.0, 100.0)
    assert alpha.get_signal() > 0.0


def test_persistent_ask_dominance_negative() -> None:
    """Sustained ask dominance -> ema_ofi < 0 -> signal < 0.

    FP = ema_ofi * |ema_ofi| / ema_abs. When ema_ofi < 0, the product
    ema_ofi * |ema_ofi| is negative, so FP < 0.
    """
    alpha = FlowPersistenceAlpha()
    for _ in range(50):
        alpha.update(100.0, 500.0)
    assert alpha.get_signal() < 0.0


def test_signal_bounded_random() -> None:
    """Signal stays finite across random inputs."""
    alpha = FlowPersistenceAlpha()
    rng = np.random.default_rng(42)
    bids = rng.uniform(0, 1000, 200)
    asks = rng.uniform(0, 1000, 200)
    for b, a in zip(bids, asks):
        sig = alpha.update(b, a)
        assert np.isfinite(sig)


def test_signal_bounded_extreme_inputs() -> None:
    """Even with extreme inputs, signal stays finite."""
    alpha = FlowPersistenceAlpha()
    for _ in range(200):
        sig = alpha.update(1e9, 0.0)
        assert np.isfinite(sig)


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_convergence_constant_input() -> None:
    """Given constant input, EMAs converge and signal stabilizes.

    With constant OFI_raw = d:
      ema_ofi -> d, ema_abs -> |d|
      FP -> d * |d| / |d| = d
    """
    alpha = FlowPersistenceAlpha()
    bid, ask = 300.0, 100.0
    expected_ofi = bid - ask  # 200.0
    for _ in range(500):
        alpha.update(bid, ask)
    # Signal should converge to OFI_raw (= 200) since ema_ofi -> d, ema_abs -> |d|
    assert alpha.get_signal() == pytest.approx(expected_ofi, rel=1e-3)


def test_ema_first_step_values() -> None:
    """After first update, check values are correct (bootstrap)."""
    alpha = FlowPersistenceAlpha()
    bid, ask = 400.0, 100.0
    ofi_raw = bid - ask  # 300

    sig = alpha.update(bid, ask)

    # Bootstrap: ema_ofi = ofi_raw, ema_abs = |ofi_raw|
    # FP = ofi_raw * |ofi_raw| / |ofi_raw| = ofi_raw
    assert sig == pytest.approx(ofi_raw, abs=1e-12)


def test_ema_two_step_manual() -> None:
    """Verify two-step EMA computation manually."""
    alpha = FlowPersistenceAlpha()
    b1, a1 = 300.0, 100.0
    b2, a2 = 100.0, 300.0
    ofi1 = b1 - a1  # 200
    ofi2 = b2 - a2  # -200

    alpha.update(b1, a1)
    # Step 1 (bootstrap): ema_ofi = ofi1, ema_abs = |ofi1|

    sig2 = alpha.update(b2, a2)
    # Step 2:
    ema_ofi = ofi1 + _EMA_ALPHA_8 * (ofi2 - ofi1)
    ema_abs = abs(ofi1) + _EMA_ALPHA_16 * (abs(ofi2) - abs(ofi1))
    expected = ema_ofi * abs(ema_ofi) / max(ema_abs, _EPSILON)

    assert sig2 == pytest.approx(expected, abs=1e-12)


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
