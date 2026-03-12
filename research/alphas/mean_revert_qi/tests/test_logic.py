"""Gate B correctness tests for MeanRevertQiAlpha (ref 098)."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.mean_revert_qi.impl import ALPHA_CLASS, MeanRevertQiAlpha

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert MeanRevertQiAlpha().manifest.alpha_id == "mean_revert_qi"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert MeanRevertQiAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs_includes_098() -> None:
    assert "098" in MeanRevertQiAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = MeanRevertQiAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D."""
    assert MeanRevertQiAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert MeanRevertQiAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = MeanRevertQiAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is MeanRevertQiAlpha


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = MeanRevertQiAlpha()
    assert isinstance(alpha, AlphaProtocol)


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_equal_queues_signal_zero() -> None:
    """Equal bid and ask queues -> qi = 0, signal = 0."""
    alpha = MeanRevertQiAlpha()
    sig = alpha.update(100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-6)


def test_signal_bounded_in_minus2_to_plus2() -> None:
    """Signal must stay in [-2, 2] at all times."""
    alpha = MeanRevertQiAlpha()
    rng = np.random.default_rng(42)
    bids = rng.uniform(0, 1000, 500)
    asks = rng.uniform(0, 1000, 500)
    for b, a in zip(bids, asks):
        sig = alpha.update(b, a)
        assert -2.0 - 1e-9 <= sig <= 2.0 + 1e-9, f"Signal out of bounds: {sig}"


def test_signal_bounded_extreme_one_sided() -> None:
    """Even with extreme one-sided input, signal clips to [-2, 2]."""
    alpha = MeanRevertQiAlpha()
    for _ in range(500):
        alpha.update(10000.0, 0.0)
    assert -2.0 - 1e-9 <= alpha.get_signal() <= 2.0 + 1e-9


# ---------------------------------------------------------------------------
# Contrarian direction tests
# ---------------------------------------------------------------------------


def test_contrarian_bid_dominance_becomes_negative() -> None:
    """Sustained bid dominance -> qi stays positive -> z grows positive ->
    contrarian signal becomes negative (mean-reversion expects correction)."""
    alpha = MeanRevertQiAlpha()
    # Warm up with balanced input to establish baseline
    for _ in range(200):
        alpha.update(100.0, 100.0)
    # Then shift to strong bid dominance
    for _ in range(200):
        alpha.update(300.0, 50.0)
    # z-score should be positive (qi > long_ema), signal negated -> negative
    assert alpha.get_signal() < 0.0, f"Expected negative contrarian signal, got {alpha.get_signal()}"


def test_contrarian_ask_dominance_becomes_positive() -> None:
    """Sustained ask dominance after balanced period -> positive contrarian signal."""
    alpha = MeanRevertQiAlpha()
    for _ in range(200):
        alpha.update(100.0, 100.0)
    for _ in range(200):
        alpha.update(50.0, 300.0)
    assert alpha.get_signal() > 0.0, f"Expected positive contrarian signal, got {alpha.get_signal()}"


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_long_ema_converges_to_constant_qi() -> None:
    """With constant input, long_ema converges to the raw qi."""
    alpha = MeanRevertQiAlpha()
    bid, ask = 300.0, 100.0
    for _ in range(2000):
        alpha.update(bid, ask)
    # After convergence, qi == long_ema, so z ~ 0, signal ~ 0
    assert alpha.get_signal() == pytest.approx(0.0, abs=0.05)


def test_variance_ema_decreases_with_constant_input() -> None:
    """With constant input, variance EMA converges toward zero."""
    alpha = MeanRevertQiAlpha()
    bid, ask = 300.0, 100.0
    for _ in range(2000):
        alpha.update(bid, ask)
    # var_ema should be very small (approaching zero)
    assert alpha._var_ema < 0.001


def test_signal_responds_to_regime_change() -> None:
    """Signal should spike when input regime changes abruptly."""
    alpha = MeanRevertQiAlpha()
    # Establish stable regime
    for _ in range(500):
        alpha.update(100.0, 100.0)
    sig_before = alpha.get_signal()
    # Abrupt shift
    for _ in range(5):
        alpha.update(400.0, 50.0)
    sig_after = alpha.get_signal()
    # Signal magnitude should increase
    assert abs(sig_after) > abs(sig_before)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = MeanRevertQiAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    assert isinstance(sig, float)


def test_update_accepts_bids_asks_arrays() -> None:
    alpha = MeanRevertQiAlpha()
    bids = np.array([[100.0, 500.0], [99.0, 300.0]])
    asks = np.array([[101.0, 200.0], [102.0, 150.0]])
    sig = alpha.update(bids=bids, asks=asks)
    assert isinstance(sig, float)


def test_reset_clears_state() -> None:
    alpha = MeanRevertQiAlpha()
    for _ in range(100):
        alpha.update(800.0, 100.0)
    alpha.reset()
    # After reset, first update with equal queues -> signal ~ 0
    sig = alpha.update(300.0, 300.0)
    assert sig == pytest.approx(0.0, abs=1e-6)


def test_get_signal_before_update_is_zero() -> None:
    alpha = MeanRevertQiAlpha()
    assert alpha.get_signal() == 0.0


def test_reset_zeroes_all_state() -> None:
    alpha = MeanRevertQiAlpha()
    for _ in range(50):
        alpha.update(500.0, 50.0)
    alpha.reset()
    assert alpha._long_ema == 0.0
    assert alpha._var_ema == 0.0
    assert alpha._signal == 0.0


def test_manifest_complexity() -> None:
    assert MeanRevertQiAlpha().manifest.complexity == "O(1)"


def test_manifest_formula_contains_clip() -> None:
    """Formula should reference the clip operation."""
    assert "clip" in MeanRevertQiAlpha().manifest.formula
