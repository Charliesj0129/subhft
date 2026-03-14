"""Gate B correctness tests for MultilevelOfiAlpha (ref 124)."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.multilevel_ofi.impl import (
    ALPHA_CLASS,
    MultilevelOfiAlpha,
    _EMA_ALPHA,
    _N_LEVELS,
    _WEIGHTS,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert MultilevelOfiAlpha().manifest.alpha_id == "multilevel_ofi"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert MultilevelOfiAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs_includes_124() -> None:
    assert "124" in MultilevelOfiAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = MultilevelOfiAlpha().manifest.data_fields
    assert "bids" in fields
    assert "asks" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert MultilevelOfiAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert MultilevelOfiAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = MultilevelOfiAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is MultilevelOfiAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_equal_depth_signal_zero() -> None:
    """Equal bid and ask depth at all levels -> delta = 0 -> signal = 0."""
    alpha = MultilevelOfiAlpha()
    bids = np.array([[100, 50], [99, 40], [98, 30], [97, 20], [96, 10]], dtype=np.float64)
    asks = np.array([[101, 50], [102, 40], [103, 30], [104, 20], [105, 10]], dtype=np.float64)
    # First tick: deltas from zero -> nonzero signal
    alpha.update(bids=bids, asks=asks)
    # Second tick with same depth: deltas = 0 -> signal decays toward 0
    for _ in range(100):
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-3)


def test_bid_heavy_signal_positive() -> None:
    """Increasing bid depth -> positive OFI -> positive signal."""
    alpha = MultilevelOfiAlpha()
    base_bids = np.array([[100, 10], [99, 10], [98, 10], [97, 10], [96, 10]], dtype=np.float64)
    base_asks = np.array([[101, 10], [102, 10], [103, 10], [104, 10], [105, 10]], dtype=np.float64)
    alpha.update(bids=base_bids, asks=base_asks)
    # Now increase bids substantially
    heavy_bids = np.array([[100, 200], [99, 200], [98, 200], [97, 200], [96, 200]], dtype=np.float64)
    for _ in range(20):
        sig = alpha.update(bids=heavy_bids, asks=base_asks)
    assert sig > 0.0


def test_ask_heavy_signal_negative() -> None:
    """Increasing ask depth -> negative OFI -> negative signal."""
    alpha = MultilevelOfiAlpha()
    base_bids = np.array([[100, 10], [99, 10], [98, 10], [97, 10], [96, 10]], dtype=np.float64)
    base_asks = np.array([[101, 10], [102, 10], [103, 10], [104, 10], [105, 10]], dtype=np.float64)
    alpha.update(bids=base_bids, asks=base_asks)
    # Now increase asks substantially
    heavy_asks = np.array(
        [[101, 200], [102, 200], [103, 200], [104, 200], [105, 200]], dtype=np.float64
    )
    for _ in range(20):
        sig = alpha.update(bids=base_bids, asks=heavy_asks)
    assert sig < 0.0


def test_signal_clipped_to_bounds() -> None:
    """Signal must stay in [-2, 2] even with extreme inputs."""
    alpha = MultilevelOfiAlpha()
    rng = np.random.default_rng(42)
    for _ in range(200):
        bids = np.column_stack(
            [
                np.arange(100, 95, -1, dtype=np.float64),
                rng.uniform(0, 10000, 5),
            ]
        )
        asks = np.column_stack(
            [
                np.arange(101, 106, dtype=np.float64),
                rng.uniform(0, 10000, 5),
            ]
        )
        sig = alpha.update(bids=bids, asks=asks)
        assert -2.0 <= sig <= 2.0


# ---------------------------------------------------------------------------
# Multi-level weighting tests
# ---------------------------------------------------------------------------


def test_deeper_levels_have_less_weight() -> None:
    """Changes at deeper levels should produce a smaller signal than L1 changes."""
    # Use small quantities to avoid hitting the [-2, 2] clip boundary
    # Alpha 1: change only L1
    a1 = MultilevelOfiAlpha()
    bids_base = np.zeros((5, 2), dtype=np.float64)
    asks_base = np.zeros((5, 2), dtype=np.float64)
    a1.update(bids=bids_base, asks=asks_base)
    bids_l1 = bids_base.copy()
    bids_l1[0, 1] = 1.0  # L1 bid increase (small to stay within clip)
    sig_l1 = a1.update(bids=bids_l1, asks=asks_base)

    # Alpha 2: change only L5
    a2 = MultilevelOfiAlpha()
    a2.update(bids=bids_base, asks=asks_base)
    bids_l5 = bids_base.copy()
    bids_l5[4, 1] = 1.0  # L5 bid increase
    sig_l5 = a2.update(bids=bids_l5, asks=asks_base)

    assert abs(sig_l1) > abs(sig_l5)
    # The ratio should match the weight ratio
    expected_ratio = _WEIGHTS[0] / _WEIGHTS[4]
    actual_ratio = abs(sig_l1) / abs(sig_l5)
    assert actual_ratio == pytest.approx(expected_ratio, rel=1e-6)


def test_weights_are_exponentially_decaying() -> None:
    """Verify weight values match exp(-0.5*k) for k=0..4."""
    for k in range(_N_LEVELS):
        assert _WEIGHTS[k] == pytest.approx(math.exp(-0.5 * k), abs=1e-6)


def test_all_levels_contribute() -> None:
    """Signal from changes across all 5 levels should be larger than L1 alone."""
    # Use small quantities to avoid hitting the [-2, 2] clip boundary
    # Alpha 1: change all levels
    a1 = MultilevelOfiAlpha()
    bids_base = np.zeros((5, 2), dtype=np.float64)
    asks_base = np.zeros((5, 2), dtype=np.float64)
    a1.update(bids=bids_base, asks=asks_base)
    bids_all = np.zeros((5, 2), dtype=np.float64)
    bids_all[:, 1] = 0.5  # small to stay within clip
    sig_all = a1.update(bids=bids_all, asks=asks_base)

    # Alpha 2: change only L1
    a2 = MultilevelOfiAlpha()
    a2.update(bids=bids_base, asks=asks_base)
    bids_l1_only = np.zeros((5, 2), dtype=np.float64)
    bids_l1_only[0, 1] = 0.5
    sig_l1 = a2.update(bids=bids_l1_only, asks=asks_base)

    assert abs(sig_all) > abs(sig_l1)


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_converges_to_zero_constant_depth() -> None:
    """Constant depth -> delta = 0 each tick -> EMA decays to 0."""
    alpha = MultilevelOfiAlpha()
    bids = np.array([[100, 50], [99, 40], [98, 30], [97, 20], [96, 10]], dtype=np.float64)
    asks = np.array([[101, 50], [102, 40], [103, 30], [104, 20], [105, 10]], dtype=np.float64)
    alpha.update(bids=bids, asks=asks)  # first tick: nonzero delta
    for _ in range(200):
        alpha.update(bids=bids, asks=asks)  # delta = 0 each tick
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-6)


def test_ema_single_step_initializes_to_raw() -> None:
    """First update initializes EMA to the raw weighted OFI (no prior history)."""
    alpha = MultilevelOfiAlpha()
    # Use small quantities to stay within [-2, 2] clip range
    bids = np.array([[100, 0.8], [99, 0.6], [98, 0.4], [97, 0.2], [96, 0.1]], dtype=np.float64)
    asks = np.array(
        [[101, 0.3], [102, 0.2], [103, 0.1], [104, 0.05], [105, 0.02]],
        dtype=np.float64,
    )
    sig = alpha.update(bids=bids, asks=asks)
    # Expected: weighted sum of (bid_qty - 0) - (ask_qty - 0) per level
    delta = bids[:, 1] - asks[:, 1]
    expected = float(np.dot(_WEIGHTS, delta))
    assert sig == pytest.approx(expected, abs=1e-9)


def test_ema_decay_second_step() -> None:
    """Second step: EMA = prev + alpha*(raw - prev)."""
    alpha = MultilevelOfiAlpha()
    # Use small quantities to stay within [-2, 2] clip range
    bids1 = np.array([[100, 0.8], [99, 0.6], [98, 0.4], [97, 0.2], [96, 0.1]], dtype=np.float64)
    asks1 = np.array(
        [[101, 0.3], [102, 0.2], [103, 0.1], [104, 0.05], [105, 0.02]],
        dtype=np.float64,
    )
    sig1 = alpha.update(bids=bids1, asks=asks1)

    bids2 = np.array([[100, 0.4], [99, 0.3], [98, 0.2], [97, 0.1], [96, 0.05]], dtype=np.float64)
    asks2 = np.array(
        [[101, 0.6], [102, 0.5], [103, 0.4], [104, 0.3], [105, 0.2]],
        dtype=np.float64,
    )
    sig2 = alpha.update(bids=bids2, asks=asks2)

    delta2 = (bids2[:, 1] - bids1[:, 1]) - (asks2[:, 1] - asks1[:, 1])
    raw2 = float(np.dot(_WEIGHTS, delta2))
    expected = sig1 + _EMA_ALPHA * (raw2 - sig1)
    assert sig2 == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_bid_ask_qty() -> None:
    """Fallback: bid_qty/ask_qty kwargs -> L1 only."""
    alpha = MultilevelOfiAlpha()
    # Use small quantities to stay within [-2, 2] clip range
    sig = alpha.update(bid_qty=1.5, ask_qty=0.5)
    # First tick: delta from zero -> weighted L1 only
    expected = _WEIGHTS[0] * (1.5 - 0.5)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_update_accepts_positional_args() -> None:
    """Positional: bid_qty, ask_qty -> L1 only."""
    alpha = MultilevelOfiAlpha()
    sig = alpha.update(1.5, 0.5)
    expected = _WEIGHTS[0] * (1.5 - 0.5)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_update_fewer_than_5_levels() -> None:
    """bids/asks with fewer than 5 levels should work (zero-padded)."""
    alpha = MultilevelOfiAlpha()
    # Use small quantities to stay within [-2, 2] clip range
    bids = np.array([[100, 0.8], [99, 0.6]], dtype=np.float64)
    asks = np.array([[101, 0.3]], dtype=np.float64)
    sig = alpha.update(bids=bids, asks=asks)
    # Only provided levels contribute; rest are zero
    expected = _WEIGHTS[0] * (0.8 - 0.3) + _WEIGHTS[1] * (0.6 - 0.0)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_reset_clears_state() -> None:
    alpha = MultilevelOfiAlpha()
    bids = np.array([[100, 200], [99, 100], [98, 50], [97, 25], [96, 10]], dtype=np.float64)
    asks = np.array([[101, 30], [102, 20], [103, 10], [104, 5], [105, 2]], dtype=np.float64)
    alpha.update(bids=bids, asks=asks)
    alpha.reset()
    # After reset, prev depth should be zero -> same as fresh instance
    alpha2 = MultilevelOfiAlpha()
    sig1 = alpha.update(bids=bids, asks=asks)
    sig2 = alpha2.update(bids=bids, asks=asks)
    assert sig1 == pytest.approx(sig2, abs=1e-9)


def test_get_signal_before_update_is_zero() -> None:
    alpha = MultilevelOfiAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = MultilevelOfiAlpha()
    assert isinstance(alpha, AlphaProtocol)
