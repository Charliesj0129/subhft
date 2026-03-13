"""Gate B correctness tests for PregeometricLobAlpha (ref 092)."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.pregeometric_lob.impl import (
    ALPHA_CLASS,
    PregeometricLobAlpha,
    _EMA_ALPHA,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert PregeometricLobAlpha().manifest.alpha_id == "pregeometric_lob"


def test_manifest_tier_is_ensemble() -> None:
    from research.registry.schemas import AlphaTier

    assert PregeometricLobAlpha().manifest.tier == AlphaTier.ENSEMBLE


def test_manifest_paper_refs_includes_092() -> None:
    assert "092" in PregeometricLobAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = PregeometricLobAlpha().manifest.data_fields
    assert "bids" in fields
    assert "asks" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert PregeometricLobAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert PregeometricLobAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = PregeometricLobAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is PregeometricLobAlpha


# ---------------------------------------------------------------------------
# Gamma shape estimator unit tests
# ---------------------------------------------------------------------------


def test_gamma_shape_uniform_quantities() -> None:
    """All equal quantities -> mean^2/var = inf; capped at 1.0 (zero var)."""
    qtys = np.array([100.0, 100.0, 100.0, 100.0])
    shape = PregeometricLobAlpha._estimate_gamma_shape(qtys)
    assert shape == 1.0  # zero variance guard


def test_gamma_shape_known_distribution() -> None:
    """For quantities [1, 2, 3, 4]: mean=2.5, var=1.25 -> shape=5.0."""
    qtys = np.array([1.0, 2.0, 3.0, 4.0])
    shape = PregeometricLobAlpha._estimate_gamma_shape(qtys)
    expected = 2.5**2 / 1.25  # 5.0
    assert shape == pytest.approx(expected, abs=1e-9)


def test_gamma_shape_single_level() -> None:
    """Single level -> returns 1.0 (neutral)."""
    qtys = np.array([500.0])
    shape = PregeometricLobAlpha._estimate_gamma_shape(qtys)
    assert shape == 1.0


def test_gamma_shape_empty_array() -> None:
    """Empty array -> returns 1.0 (neutral)."""
    qtys = np.array([])
    shape = PregeometricLobAlpha._estimate_gamma_shape(qtys)
    assert shape == 1.0


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_symmetric_book_signal_zero() -> None:
    """Symmetric bid/ask depth -> gamma shapes equal -> signal = 0."""
    alpha = PregeometricLobAlpha()
    bids = np.array([[100, 200], [99, 150], [98, 100]])
    asks = np.array([[101, 200], [102, 150], [103, 100]])
    sig = alpha.update(bids=bids, asks=asks)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_concentrated_bid_positive_signal() -> None:
    """Concentrated bid (low var) vs dispersed ask -> positive signal."""
    alpha = PregeometricLobAlpha()
    # Bid: uniform quantities (high shape = concentrated)
    # Ask: varied quantities (low shape = dispersed)
    bids = np.array([[100, 200], [99, 198], [98, 201]])
    asks = np.array([[101, 50], [102, 400], [103, 10]])
    for _ in range(30):
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() > 0.0


def test_concentrated_ask_negative_signal() -> None:
    """Concentrated ask (low var) vs dispersed bid -> negative signal."""
    alpha = PregeometricLobAlpha()
    bids = np.array([[100, 50], [99, 400], [98, 10]])
    asks = np.array([[101, 200], [102, 198], [103, 201]])
    for _ in range(30):
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() < 0.0


def test_signal_bounded_minus2_to_plus2() -> None:
    """Signal must stay in [-2, 2] even with extreme inputs."""
    alpha = PregeometricLobAlpha()
    rng = np.random.default_rng(42)
    for _ in range(200):
        bids = np.column_stack(
            [np.arange(5), rng.exponential(100, 5)]
        )
        asks = np.column_stack(
            [np.arange(5), rng.exponential(100, 5)]
        )
        sig = alpha.update(bids=bids, asks=asks)
        assert -2.0 <= sig <= 2.0


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_first_tick_initializes_to_raw_diff() -> None:
    """First update initializes EMA to raw shape_diff (no prior)."""
    alpha = PregeometricLobAlpha()
    bids = np.array([[100, 1.0], [99, 2.0], [98, 3.0], [97, 4.0]])
    asks = np.array([[101, 5.0], [102, 6.0], [103, 7.0], [104, 8.0]])
    bid_shape = PregeometricLobAlpha._estimate_gamma_shape(np.array([1.0, 2.0, 3.0, 4.0]))
    ask_shape = PregeometricLobAlpha._estimate_gamma_shape(np.array([5.0, 6.0, 7.0, 8.0]))
    expected = bid_shape - ask_shape
    expected = max(-2.0, min(2.0, expected))
    sig = alpha.update(bids=bids, asks=asks)
    assert sig == pytest.approx(expected, abs=1e-6)


def test_ema_converges_constant_input() -> None:
    """EMA should converge to the raw shape_diff given constant input."""
    alpha = PregeometricLobAlpha()
    bids = np.array([[100, 100], [99, 200], [98, 300]])
    asks = np.array([[101, 50], [102, 100], [103, 150]])
    bid_shape = PregeometricLobAlpha._estimate_gamma_shape(
        np.array([100.0, 200.0, 300.0])
    )
    ask_shape = PregeometricLobAlpha._estimate_gamma_shape(
        np.array([50.0, 100.0, 150.0])
    )
    expected_diff = bid_shape - ask_shape
    for _ in range(200):
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() == pytest.approx(
        max(-2.0, min(2.0, expected_diff)), abs=1e-4
    )


def test_ema_decay_second_step() -> None:
    """Second step: EMA = prev + alpha * (raw - prev)."""
    alpha = PregeometricLobAlpha()
    bids1 = np.array([[100, 100], [99, 200], [98, 300]])
    asks1 = np.array([[101, 100], [102, 200], [103, 300]])
    bids2 = np.array([[100, 50], [99, 400], [98, 10]])
    asks2 = np.array([[101, 200], [102, 198], [103, 201]])

    diff1 = (
        PregeometricLobAlpha._estimate_gamma_shape(np.array([100.0, 200.0, 300.0]))
        - PregeometricLobAlpha._estimate_gamma_shape(np.array([100.0, 200.0, 300.0]))
    )
    diff2 = (
        PregeometricLobAlpha._estimate_gamma_shape(np.array([50.0, 400.0, 10.0]))
        - PregeometricLobAlpha._estimate_gamma_shape(np.array([200.0, 198.0, 201.0]))
    )
    expected_ema2 = diff1 + _EMA_ALPHA * (diff2 - diff1)

    alpha.update(bids=bids1, asks=asks1)
    sig2 = alpha.update(bids=bids2, asks=asks2)
    assert sig2 == pytest.approx(max(-2.0, min(2.0, expected_ema2)), abs=1e-6)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_positional_fallback_returns_float() -> None:
    """Positional bid_qty/ask_qty fallback returns 0 signal (no depth)."""
    alpha = PregeometricLobAlpha()
    sig = alpha.update(200.0, 100.0)
    assert isinstance(sig, float)
    assert sig == 0.0


def test_update_keyword_fallback_returns_float() -> None:
    """Keyword bid_qty/ask_qty fallback returns 0 signal (no depth)."""
    alpha = PregeometricLobAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    assert isinstance(sig, float)
    assert sig == 0.0


def test_update_no_args_returns_float() -> None:
    """update() with no args returns current signal (zero initially)."""
    alpha = PregeometricLobAlpha()
    sig = alpha.update()
    assert isinstance(sig, float)
    assert sig == 0.0


def test_reset_clears_state() -> None:
    alpha = PregeometricLobAlpha()
    bids = np.array([[100, 100], [99, 500]])
    asks = np.array([[101, 50], [102, 100]])
    alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() != 0.0
    alpha.reset()
    assert alpha.get_signal() == 0.0
    assert alpha._initialized is False


def test_get_signal_before_update_is_zero() -> None:
    alpha = PregeometricLobAlpha()
    assert alpha.get_signal() == 0.0


def test_empty_bids_array_returns_current_signal() -> None:
    """Empty bid array should not crash — returns current signal."""
    alpha = PregeometricLobAlpha()
    sig = alpha.update(bids=np.array([]), asks=np.array([[101, 100]]))
    assert isinstance(sig, float)


def test_empty_asks_array_returns_current_signal() -> None:
    """Empty ask array should not crash — returns current signal."""
    alpha = PregeometricLobAlpha()
    sig = alpha.update(bids=np.array([[100, 100]]), asks=np.array([]))
    assert isinstance(sig, float)


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = PregeometricLobAlpha()
    assert isinstance(alpha, AlphaProtocol)
