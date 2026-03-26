"""Gate B correctness tests for MultiLevelDepthMomentumAlpha."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.mldm_depth_momentum.impl import (
    ALPHA_CLASS,
    MultiLevelDepthMomentumAlpha,
    _EMA_FAST,
    _EMA_OUTPUT,
    _EMA_SLOW,
    _N_LEVELS,
    _SIGNAL_CLIP,
    _WARMUP_TICKS,
)


def _make_book(bid_qtys: list[float], ask_qtys: list[float]) -> tuple[np.ndarray, np.ndarray]:
    bids = np.zeros((5, 2), dtype=np.float64)
    asks = np.zeros((5, 2), dtype=np.float64)
    for i, q in enumerate(bid_qtys):
        bids[i] = [100 - i, q]
    for i, q in enumerate(ask_qtys):
        asks[i] = [101 + i, q]
    return bids, asks


def _warmup(alpha: MultiLevelDepthMomentumAlpha, n: int = _WARMUP_TICKS + 5) -> None:
    bids, asks = _make_book([10] * 5, [10] * 5)
    for _ in range(n):
        alpha.update(bids=bids, asks=asks)


# --- Manifest ---
def test_manifest_alpha_id() -> None:
    assert MultiLevelDepthMomentumAlpha().manifest.alpha_id == "mldm_depth_momentum"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier
    assert MultiLevelDepthMomentumAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs() -> None:
    refs = MultiLevelDepthMomentumAlpha().manifest.paper_refs
    assert "arXiv:2306.05479" in refs


def test_manifest_data_fields() -> None:
    f = MultiLevelDepthMomentumAlpha().manifest.data_fields
    assert "bids" in f and "asks" in f


def test_manifest_latency_profile_set() -> None:
    assert MultiLevelDepthMomentumAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert MultiLevelDepthMomentumAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS
    m = MultiLevelDepthMomentumAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is MultiLevelDepthMomentumAlpha


# --- Warmup ---
def test_warmup_returns_zero() -> None:
    alpha = MultiLevelDepthMomentumAlpha()
    asks_base = _make_book([10] * 5, [10] * 5)[1]
    for i in range(_WARMUP_TICKS - 1):
        deep_qty = 10 + 2 * (i + 1)
        bids, _ = _make_book([10, deep_qty, deep_qty, deep_qty, deep_qty], [10] * 5)
        sig = alpha.update(bids=bids, asks=asks_base)
        assert sig == 0.0, f"tick {i+1}: expected 0.0 during warmup, got {sig}"


def test_signal_activates_after_warmup() -> None:
    alpha = MultiLevelDepthMomentumAlpha()
    asks_base = _make_book([10] * 5, [10] * 5)[1]
    _warmup(alpha, _WARMUP_TICKS + 5)
    for i in range(1, 60):
        deep_qty = 10 + 5 * i
        bids, _ = _make_book([10, deep_qty, deep_qty, deep_qty, deep_qty], [10] * 5)
        sig = alpha.update(bids=bids, asks=asks_base)
    assert sig > 0.0, "should activate positive with growing bid depth at L2-L5"


# --- Core: L1 independence ---
def test_l1_only_change_no_signal() -> None:
    """Changes only at L1 should produce zero signal (MLDM excludes L1)."""
    alpha = MultiLevelDepthMomentumAlpha()
    _warmup(alpha, _WARMUP_TICKS + 5)
    for i in range(1, 100):
        l1_qty = 10 + 5 * i
        bids, _ = _make_book([l1_qty, 10, 10, 10, 10], [10] * 5)
        asks_base = _make_book([10] * 5, [10] * 5)[1]
        alpha.update(bids=bids, asks=asks_base)
    assert alpha.get_signal() == pytest.approx(0.0, abs=0.1)


def test_deep_bid_growth_positive() -> None:
    """Growing bid depth at L2-L5 should produce positive signal."""
    alpha = MultiLevelDepthMomentumAlpha()
    _warmup(alpha, _WARMUP_TICKS + 5)
    asks_base = _make_book([10] * 5, [10] * 5)[1]
    for i in range(1, 50):
        deep_qty = 10 + 3 * i
        bids, _ = _make_book([10, deep_qty, deep_qty, deep_qty, deep_qty], [10] * 5)
        alpha.update(bids=bids, asks=asks_base)
    assert alpha.get_signal() > 0.0


def test_deep_ask_growth_negative() -> None:
    """Growing ask depth at L2-L5 should produce negative signal."""
    alpha = MultiLevelDepthMomentumAlpha()
    bids_base = _make_book([10] * 5, [10] * 5)[0]
    _warmup(alpha, _WARMUP_TICKS + 5)
    for i in range(1, 50):
        deep_qty = 10 + 3 * i
        _, asks = _make_book([10] * 5, [10, deep_qty, deep_qty, deep_qty, deep_qty])
        alpha.update(bids=bids_base, asks=asks)
    assert alpha.get_signal() < 0.0


def test_symmetric_growth_near_zero() -> None:
    """Equal bid and ask depth growth at L2-L5 should produce near-zero signal."""
    alpha = MultiLevelDepthMomentumAlpha()
    _warmup(alpha, _WARMUP_TICKS + 5)
    for i in range(1, 100):
        deep_qty = 10 + 3 * i
        bids, asks = _make_book(
            [10, deep_qty, deep_qty, deep_qty, deep_qty],
            [10, deep_qty, deep_qty, deep_qty, deep_qty],
        )
        alpha.update(bids=bids, asks=asks)
    assert abs(alpha.get_signal()) < 0.5


# --- Boundary conditions ---
def test_equal_depth_signal_zero() -> None:
    alpha = MultiLevelDepthMomentumAlpha()
    bids, asks = _make_book([50, 40, 30, 20, 10], [50, 40, 30, 20, 10])
    for _ in range(_WARMUP_TICKS + 200):
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-3)


def test_signal_clipped_to_bounds() -> None:
    alpha = MultiLevelDepthMomentumAlpha()
    rng = np.random.default_rng(42)
    for _ in range(300):
        bids = np.column_stack([np.arange(100, 95, -1, dtype=np.float64), rng.uniform(0, 10000, 5)])
        asks = np.column_stack([np.arange(101, 106, dtype=np.float64), rng.uniform(0, 10000, 5)])
        sig = alpha.update(bids=bids, asks=asks)
        assert -_SIGNAL_CLIP <= sig <= _SIGNAL_CLIP


def test_bbo_shift_zeros_signal() -> None:
    """When BBO price changes, MLDM should zero input to avoid level-shift artifacts."""
    alpha = MultiLevelDepthMomentumAlpha()
    _warmup(alpha, _WARMUP_TICKS + 5)
    # Feed stable deep bid growth
    asks_base = _make_book([10] * 5, [10] * 5)[1]
    for i in range(1, 20):
        bids, _ = _make_book([10, 10 + 3 * i, 10 + 3 * i, 10 + 3 * i, 10 + 3 * i], [10] * 5)
        alpha.update(bids=bids, asks=asks_base)
    sig_before = alpha.get_signal()
    # Now shift BBO (price change: 100->101)
    bids_shifted = np.zeros((5, 2), dtype=np.float64)
    for i in range(5):
        bids_shifted[i] = [101 - i, 10 + 100]  # big qty change due to level shift
    alpha.update(bids=bids_shifted, asks=asks_base)
    sig_after = alpha.get_signal()
    # Signal should not jump wildly — the guard should have zeroed deep_net
    assert abs(sig_after - sig_before) < abs(sig_before) + 0.5


def test_no_bids_asks_raises() -> None:
    with pytest.raises(ValueError, match="requires bids= and asks="):
        MultiLevelDepthMomentumAlpha().update(50.0, 30.0)


def test_fewer_than_5_levels_works() -> None:
    alpha = MultiLevelDepthMomentumAlpha()
    bids = np.array([[100, 50], [99, 40]], dtype=np.float64)
    asks = np.array([[101, 30]], dtype=np.float64)
    assert isinstance(alpha.update(bids=bids, asks=asks), float)


# --- Reset / state ---
def test_reset_clears_state() -> None:
    alpha = MultiLevelDepthMomentumAlpha()
    bids, asks = _make_book([200, 100, 50, 25, 10], [30, 20, 10, 5, 2])
    alpha.update(bids=bids, asks=asks)
    alpha.reset()
    alpha2 = MultiLevelDepthMomentumAlpha()
    assert alpha.update(bids=bids, asks=asks) == pytest.approx(
        alpha2.update(bids=bids, asks=asks), abs=1e-9
    )


def test_get_signal_before_update() -> None:
    assert MultiLevelDepthMomentumAlpha().get_signal() == 0.0


# --- EMA convergence ---
def test_ema_converges_to_zero() -> None:
    alpha = MultiLevelDepthMomentumAlpha()
    bids, asks = _make_book([50, 40, 30, 20, 10], [50, 40, 30, 20, 10])
    for _ in range(1000):
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-6)


def test_first_tick_signal_is_zero() -> None:
    alpha = MultiLevelDepthMomentumAlpha()
    bids, asks = _make_book([50, 40, 30, 20, 10], [30, 20, 10, 5, 2])
    assert alpha.update(bids=bids, asks=asks) == 0.0


# --- Numerical ---
def test_zero_depth_no_crash() -> None:
    alpha = MultiLevelDepthMomentumAlpha()
    bids, asks = _make_book([0] * 5, [0] * 5)
    for _ in range(_WARMUP_TICKS + 10):
        sig = alpha.update(bids=bids, asks=asks)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_large_values_no_overflow() -> None:
    alpha = MultiLevelDepthMomentumAlpha()
    bids, asks = _make_book([1e12] * 5, [1e12] * 5)
    for _ in range(_WARMUP_TICKS + 5):
        alpha.update(bids=bids, asks=asks)
    bids2, _ = _make_book([1e12 + 1e6] * 5, [1e12] * 5)
    assert math.isfinite(alpha.update(bids=bids2, asks=asks))


# --- Protocol ---
def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol
    assert isinstance(MultiLevelDepthMomentumAlpha(), AlphaProtocol)
