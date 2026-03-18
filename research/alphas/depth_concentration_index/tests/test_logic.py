"""Gate B — correctness tests for depth_concentration_index."""
from __future__ import annotations

import math

import numpy as np
import pytest

from research.alphas.depth_concentration_index.impl import (
    ALPHA_CLASS,
    DepthConcentrationIndexAlpha,
    _hhi,
)
from research.registry.schemas import AlphaManifest, AlphaProtocol, AlphaStatus


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------

def test_manifest_alpha_id() -> None:
    alpha = DepthConcentrationIndexAlpha()
    assert alpha.manifest.alpha_id == "depth_concentration_index"


def test_manifest_status_is_draft() -> None:
    alpha = DepthConcentrationIndexAlpha()
    assert alpha.manifest.status == AlphaStatus.DRAFT


def test_manifest_data_fields() -> None:
    alpha = DepthConcentrationIndexAlpha()
    assert "bids" in alpha.manifest.data_fields
    assert "asks" in alpha.manifest.data_fields


def test_manifest_latency_profile_set() -> None:
    alpha = DepthConcentrationIndexAlpha()
    assert alpha.manifest.latency_profile is not None
    assert "shioaji" in alpha.manifest.latency_profile


def test_manifest_feature_set_version() -> None:
    alpha = DepthConcentrationIndexAlpha()
    assert alpha.manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    alpha = DepthConcentrationIndexAlpha()
    m = alpha.manifest
    assert len(m.roles_used) > 0
    assert len(m.skills_used) > 0


def test_manifest_paper_refs() -> None:
    alpha = DepthConcentrationIndexAlpha()
    assert len(alpha.manifest.paper_refs) >= 1


def test_manifest_complexity() -> None:
    alpha = DepthConcentrationIndexAlpha()
    assert alpha.manifest.complexity in ("O(1)", "O(L)", "O(N)")


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is DepthConcentrationIndexAlpha


def test_implements_alpha_protocol() -> None:
    alpha = DepthConcentrationIndexAlpha()
    assert isinstance(alpha, AlphaProtocol)


def test_manifest_is_alpha_manifest_instance() -> None:
    alpha = DepthConcentrationIndexAlpha()
    assert isinstance(alpha.manifest, AlphaManifest)


# ---------------------------------------------------------------------------
# HHI helper
# ---------------------------------------------------------------------------

def test_hhi_single_level() -> None:
    """All depth at one level => HHI = 1.0."""
    assert _hhi((100.0,)) == pytest.approx(1.0)


def test_hhi_uniform_two_levels() -> None:
    """Equal depth across 2 levels => HHI = 0.5."""
    assert _hhi((50.0, 50.0)) == pytest.approx(0.5)


def test_hhi_uniform_five_levels() -> None:
    """Equal depth across 5 levels => HHI = 0.2."""
    assert _hhi((20.0, 20.0, 20.0, 20.0, 20.0)) == pytest.approx(0.2)


def test_hhi_concentrated() -> None:
    """90% at first level => HHI >> 0.2."""
    result = _hhi((90.0, 2.5, 2.5, 2.5, 2.5))
    assert result > 0.8


def test_hhi_empty_returns_zero() -> None:
    """All zeros => HHI = 0.0 (no depth)."""
    assert _hhi((0.0, 0.0, 0.0)) == 0.0


def test_hhi_single_nonzero() -> None:
    """Only one nonzero entry => HHI = 1.0."""
    assert _hhi((0.0, 0.0, 100.0, 0.0)) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------

def _make_book(bid_qtys: list[float], ask_qtys: list[float]) -> dict:
    """Helper: create bids/asks arrays from quantity lists."""
    n_bid = len(bid_qtys)
    n_ask = len(ask_qtys)
    bids = np.zeros((n_bid, 2))
    asks = np.zeros((n_ask, 2))
    for i, q in enumerate(bid_qtys):
        bids[i, 0] = 100.0 - i * 0.5  # descending bid prices
        bids[i, 1] = q
    for i, q in enumerate(ask_qtys):
        asks[i, 0] = 100.5 + i * 0.5  # ascending ask prices
        asks[i, 1] = q
    return {"bids": bids, "asks": asks}


def test_symmetric_book_signal_zero() -> None:
    """Identical bid/ask distribution => signal ~ 0."""
    alpha = DepthConcentrationIndexAlpha()
    book = _make_book([100, 80, 60, 40, 20], [100, 80, 60, 40, 20])
    sig = alpha.update(**book)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_concentrated_asks_positive_signal() -> None:
    """Ask side concentrated => HHI_ask > HHI_bid => positive signal."""
    alpha = DepthConcentrationIndexAlpha()
    book = _make_book(
        [20, 20, 20, 20, 20],  # uniform bids => HHI = 0.2
        [100, 0, 0, 0, 0],     # concentrated asks => HHI = 1.0
    )
    sig = alpha.update(**book)
    assert sig > 0.0  # bullish: asks are fragile


def test_concentrated_bids_negative_signal() -> None:
    """Bid side concentrated => HHI_bid > HHI_ask => negative signal."""
    alpha = DepthConcentrationIndexAlpha()
    book = _make_book(
        [100, 0, 0, 0, 0],     # concentrated bids => HHI = 1.0
        [20, 20, 20, 20, 20],  # uniform asks => HHI = 0.2
    )
    sig = alpha.update(**book)
    assert sig < 0.0  # bearish: bids are fragile


def test_signal_magnitude_proportional_to_asymmetry() -> None:
    """Stronger asymmetry => larger signal magnitude."""
    alpha1 = DepthConcentrationIndexAlpha()
    alpha2 = DepthConcentrationIndexAlpha()

    # Mild asymmetry
    book_mild = _make_book([60, 40, 30, 20, 10], [80, 30, 20, 15, 5])
    sig_mild = alpha1.update(**book_mild)

    # Strong asymmetry
    book_strong = _make_book([20, 20, 20, 20, 20], [100, 0, 0, 0, 0])
    sig_strong = alpha2.update(**book_strong)

    assert abs(sig_strong) > abs(sig_mild)


# ---------------------------------------------------------------------------
# EMA convergence
# ---------------------------------------------------------------------------

def test_ema_single_step_initializes_to_raw() -> None:
    """First update sets EMA to raw HHI difference."""
    alpha = DepthConcentrationIndexAlpha()
    book = _make_book([20, 20, 20, 20, 20], [100, 0, 0, 0, 0])
    sig = alpha.update(**book)
    expected_raw = 1.0 - 0.2  # HHI_ask=1.0, HHI_bid=0.2
    assert sig == pytest.approx(expected_raw)


def test_ema_converges_constant_input() -> None:
    """Repeated identical input => EMA converges to raw value."""
    alpha = DepthConcentrationIndexAlpha()
    book = _make_book([60, 40, 30, 20, 10], [90, 30, 10, 5, 5])

    for _ in range(200):
        sig = alpha.update(**book)

    # Compute expected raw
    bid_qtys = (60.0, 40.0, 30.0, 20.0, 10.0)
    ask_qtys = (90.0, 30.0, 10.0, 5.0, 5.0)
    expected = _hhi(ask_qtys) - _hhi(bid_qtys)
    assert sig == pytest.approx(expected, abs=1e-6)


def test_ema_decay_second_step() -> None:
    """Second step applies EMA formula correctly."""
    alpha = DepthConcentrationIndexAlpha()
    ema_alpha = 1.0 - math.exp(-1.0 / 16.0)

    book1 = _make_book([20, 20, 20, 20, 20], [100, 0, 0, 0, 0])
    sig1 = alpha.update(**book1)  # raw1 = 0.8

    book2 = _make_book([100, 0, 0, 0, 0], [20, 20, 20, 20, 20])
    sig2 = alpha.update(**book2)  # raw2 = -0.8

    expected = sig1 + ema_alpha * (-0.8 - sig1)
    assert sig2 == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------

def test_update_accepts_bid_qtys_ask_qtys_kwargs() -> None:
    """Alternative API: pass pre-extracted quantity vectors."""
    alpha = DepthConcentrationIndexAlpha()
    sig = alpha.update(
        bid_qtys=[100, 80, 60, 40, 20],
        ask_qtys=[100, 80, 60, 40, 20],
    )
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_update_missing_args_returns_current_signal() -> None:
    """Calling update() with no valid args returns current signal (graceful degradation)."""
    alpha = DepthConcentrationIndexAlpha()
    sig = alpha.update()
    assert sig == 0.0  # initial signal before any data


def test_update_l1_fallback() -> None:
    """L1 fallback: bid_qty/ask_qty kwargs produce valid signal."""
    alpha = DepthConcentrationIndexAlpha()
    # bid_qty > ask_qty => ask side smaller => negative (bearish)
    sig = alpha.update(bid_qty=500.0, ask_qty=200.0)
    assert sig < 0.0

    alpha2 = DepthConcentrationIndexAlpha()
    # ask_qty > bid_qty => ask side larger => positive (bullish)
    sig2 = alpha2.update(bid_qty=200.0, ask_qty=800.0)
    assert sig2 > 0.0


def test_reset_clears_state() -> None:
    alpha = DepthConcentrationIndexAlpha()
    book = _make_book([20, 20, 20, 20, 20], [100, 0, 0, 0, 0])
    alpha.update(**book)
    assert alpha.get_signal() != 0.0

    alpha.reset()
    assert alpha.get_signal() == 0.0
    assert not alpha._initialized


def test_get_signal_before_update_is_zero() -> None:
    alpha = DepthConcentrationIndexAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_single_level_book() -> None:
    """Works with 1-level LOB (N=1 => HHI always 1.0 both sides)."""
    alpha = DepthConcentrationIndexAlpha()
    bids = np.array([[100.0, 50.0]])
    asks = np.array([[100.5, 50.0]])
    sig = alpha.update(bids=bids, asks=asks)
    assert sig == pytest.approx(0.0, abs=1e-9)  # symmetric => 0


def test_one_side_empty() -> None:
    """One side with zero depth => HHI=0 for that side."""
    alpha = DepthConcentrationIndexAlpha()
    bids = np.array([[100.0, 50.0], [99.5, 30.0]])
    asks = np.array([[100.5, 0.0], [101.0, 0.0]])
    sig = alpha.update(bids=bids, asks=asks)
    # HHI_ask = 0 (no depth), HHI_bid > 0 => signal < 0
    assert sig < 0.0


def test_large_level_count() -> None:
    """Works with many levels (e.g., 20)."""
    alpha = DepthConcentrationIndexAlpha()
    n = 20
    bids = np.column_stack([
        np.arange(100.0, 100.0 - n * 0.5, -0.5),
        np.full(n, 10.0),
    ])
    asks = np.column_stack([
        np.arange(100.5, 100.5 + n * 0.5, 0.5),
        np.full(n, 10.0),
    ])
    sig = alpha.update(bids=bids, asks=asks)
    assert sig == pytest.approx(0.0, abs=1e-9)  # symmetric
