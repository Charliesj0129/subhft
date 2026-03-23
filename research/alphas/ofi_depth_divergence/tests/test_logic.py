"""Gate B correctness tests for OfiDepthDivergenceAlpha."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.ofi_depth_divergence.impl import (
    ALPHA_CLASS,
    OfiDepthDivergenceAlpha,
    _EMA_FAST,
    _EMA_OUTPUT,
    _EMA_SLOW,
    _N_LEVELS,
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


def _warmup(alpha: OfiDepthDivergenceAlpha, n: int = _WARMUP_TICKS + 5) -> None:
    bids, asks = _make_book([10] * 5, [10] * 5)
    for _ in range(n):
        alpha.update(bids=bids, asks=asks)


# --- Manifest ---
def test_manifest_alpha_id() -> None:
    assert OfiDepthDivergenceAlpha().manifest.alpha_id == "ofi_depth_divergence"

def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier
    assert OfiDepthDivergenceAlpha().manifest.tier == AlphaTier.TIER_2

def test_manifest_paper_refs() -> None:
    assert "arXiv:2112.13213" in OfiDepthDivergenceAlpha().manifest.paper_refs

def test_manifest_data_fields() -> None:
    f = OfiDepthDivergenceAlpha().manifest.data_fields
    assert "bids" in f and "asks" in f

def test_manifest_latency_profile_set() -> None:
    assert OfiDepthDivergenceAlpha().manifest.latency_profile is not None

def test_manifest_feature_set_version() -> None:
    assert OfiDepthDivergenceAlpha().manifest.feature_set_version == "lob_shared_v1"

def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS
    m = OfiDepthDivergenceAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS

def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is OfiDepthDivergenceAlpha


# --- Warmup ---
def test_warmup_returns_zero() -> None:
    alpha = OfiDepthDivergenceAlpha()
    asks_base = _make_book([10] * 5, [10] * 5)[1]
    for i in range(_WARMUP_TICKS - 1):
        deep_qty = 10 + 2 * (i + 1)
        bids, _ = _make_book([10, 10, deep_qty, deep_qty, deep_qty], [10] * 5)
        sig = alpha.update(bids=bids, asks=asks_base)
        assert sig == 0.0, f"tick {i+1}: expected 0.0 during warmup, got {sig}"

def test_signal_activates_after_warmup() -> None:
    alpha = OfiDepthDivergenceAlpha()
    asks_base = _make_book([10] * 5, [10] * 5)[1]
    _warmup(alpha, _WARMUP_TICKS + 5)
    for i in range(1, 40):
        deep_qty = 10 + 3 * i
        bids, _ = _make_book([10, 10, deep_qty, deep_qty, deep_qty], [10] * 5)
        sig = alpha.update(bids=bids, asks=asks_base)
    assert sig < 0.0  # deep leading = negative (negated signal)


# --- Boundary conditions ---
def test_equal_depth_signal_zero() -> None:
    alpha = OfiDepthDivergenceAlpha()
    bids, asks = _make_book([50, 40, 30, 20, 10], [50, 40, 30, 20, 10])
    for _ in range(_WARMUP_TICKS + 100):
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-3)

def test_deep_leading_shallow_negative_signal() -> None:
    alpha = OfiDepthDivergenceAlpha()
    asks_base = _make_book([10] * 5, [10] * 5)[1]
    _warmup(alpha, _WARMUP_TICKS + 5)
    for i in range(1, 40):
        deep_qty = 10 + 3 * i
        bids, _ = _make_book([10, 10, deep_qty, deep_qty, deep_qty], [10] * 5)
        alpha.update(bids=bids, asks=asks_base)
    assert alpha.get_signal() < 0.0

def test_shallow_leading_deep_positive_signal() -> None:
    alpha = OfiDepthDivergenceAlpha()
    asks_base = _make_book([10] * 5, [10] * 5)[1]
    _warmup(alpha, _WARMUP_TICKS + 5)
    for i in range(1, 40):
        shallow_qty = 10 + 3 * i
        bids, _ = _make_book([shallow_qty, shallow_qty, 10, 10, 10], [10] * 5)
        alpha.update(bids=bids, asks=asks_base)
    assert alpha.get_signal() > 0.0

def test_signal_clipped_to_bounds() -> None:
    alpha = OfiDepthDivergenceAlpha()
    rng = np.random.default_rng(42)
    for _ in range(200):
        bids = np.column_stack([np.arange(100, 95, -1, dtype=np.float64), rng.uniform(0, 10000, 5)])
        asks = np.column_stack([np.arange(101, 106, dtype=np.float64), rng.uniform(0, 10000, 5)])
        sig = alpha.update(bids=bids, asks=asks)
        assert -2.0 <= sig <= 2.0


# --- Band normalization ---
def test_uniform_increase_signal_near_zero() -> None:
    alpha = OfiDepthDivergenceAlpha()
    asks_base = _make_book([10] * 5, [10] * 5)[1]
    for i in range(_WARMUP_TICKS + 50):
        uni_qty = 10 + 2 * (i + 1)
        bids, _ = _make_book([uni_qty] * 5, [10] * 5)
        alpha.update(bids=bids, asks=asks_base)
    assert abs(alpha.get_signal()) < 0.5


# --- Divergence properties ---
def test_shallow_leading_stronger_than_uniform() -> None:
    asks_base = _make_book([10] * 5, [10] * 5)[1]
    a_shallow = OfiDepthDivergenceAlpha()
    _warmup(a_shallow, _WARMUP_TICKS + 5)
    for i in range(1, 40):
        bids, _ = _make_book([10 + 3 * i, 10 + 3 * i, 10, 10, 10], [10] * 5)
        a_shallow.update(bids=bids, asks=asks_base)

    a_uni = OfiDepthDivergenceAlpha()
    _warmup(a_uni, _WARMUP_TICKS + 5)
    for i in range(1, 40):
        bids, _ = _make_book([10 + 3 * i] * 5, [10] * 5)
        a_uni.update(bids=bids, asks=asks_base)

    assert a_shallow.get_signal() > a_uni.get_signal()

def test_opposite_divergence_signs() -> None:
    asks_base = _make_book([10] * 5, [10] * 5)[1]
    a1 = OfiDepthDivergenceAlpha()
    _warmup(a1, _WARMUP_TICKS + 5)
    for i in range(1, 40):
        bids, _ = _make_book([10, 10, 10 + 3 * i, 10 + 3 * i, 10 + 3 * i], [10] * 5)
        a1.update(bids=bids, asks=asks_base)
    a2 = OfiDepthDivergenceAlpha()
    _warmup(a2, _WARMUP_TICKS + 5)
    for i in range(1, 40):
        bids, _ = _make_book([10 + 3 * i, 10 + 3 * i, 10, 10, 10], [10] * 5)
        a2.update(bids=bids, asks=asks_base)
    assert a1.get_signal() < 0.0
    assert a2.get_signal() > 0.0


# --- EMA convergence ---
def test_ema_converges_to_zero() -> None:
    alpha = OfiDepthDivergenceAlpha()
    bids, asks = _make_book([50, 40, 30, 20, 10], [50, 40, 30, 20, 10])
    for _ in range(500):
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-6)

def test_first_tick_signal_is_zero() -> None:
    alpha = OfiDepthDivergenceAlpha()
    bids, asks = _make_book([50, 40, 30, 20, 10], [30, 20, 10, 5, 2])
    assert alpha.update(bids=bids, asks=asks) == 0.0


# --- L1-only raises ---
def test_positional_args_raises() -> None:
    with pytest.raises(ValueError, match="requires bids= and asks="):
        OfiDepthDivergenceAlpha().update(50.0, 30.0)

def test_bid_ask_qty_kwargs_raises() -> None:
    with pytest.raises(ValueError, match="requires bids= and asks="):
        OfiDepthDivergenceAlpha().update(bid_qty=50.0, ask_qty=30.0)

def test_fewer_than_5_levels_works() -> None:
    alpha = OfiDepthDivergenceAlpha()
    bids = np.array([[100, 50], [99, 40]], dtype=np.float64)
    asks = np.array([[101, 30]], dtype=np.float64)
    assert isinstance(alpha.update(bids=bids, asks=asks), float)


# --- Reset / state ---
def test_reset_clears_state() -> None:
    alpha = OfiDepthDivergenceAlpha()
    bids, asks = _make_book([200, 100, 50, 25, 10], [30, 20, 10, 5, 2])
    alpha.update(bids=bids, asks=asks)
    alpha.reset()
    alpha2 = OfiDepthDivergenceAlpha()
    assert alpha.update(bids=bids, asks=asks) == pytest.approx(alpha2.update(bids=bids, asks=asks), abs=1e-9)

def test_get_signal_before_update() -> None:
    assert OfiDepthDivergenceAlpha().get_signal() == 0.0


# --- Protocol ---
def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol
    assert isinstance(OfiDepthDivergenceAlpha(), AlphaProtocol)


# --- Numerical ---
def test_zero_depth_no_crash() -> None:
    alpha = OfiDepthDivergenceAlpha()
    bids, asks = _make_book([0] * 5, [0] * 5)
    for _ in range(_WARMUP_TICKS + 10):
        sig = alpha.update(bids=bids, asks=asks)
    assert sig == pytest.approx(0.0, abs=1e-9)

def test_large_values_no_overflow() -> None:
    alpha = OfiDepthDivergenceAlpha()
    bids, asks = _make_book([1e12] * 5, [1e12] * 5)
    for _ in range(_WARMUP_TICKS + 5):
        alpha.update(bids=bids, asks=asks)
    bids2, _ = _make_book([1e12 + 1e6] * 5, [1e12] * 5)
    assert math.isfinite(alpha.update(bids=bids2, asks=asks))
