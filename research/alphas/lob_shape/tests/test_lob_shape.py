"""Gate B tests for lob_shape alpha.

TDD-first: written before impl.py. Run with:
    uv run pytest research/alphas/lob_shape/tests/ -v --no-cov
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.append(os.getcwd())

from research.alphas.lob_shape.impl import (
    LobShapeAlpha,
    _compute_slope,
    _sign_align,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_bid_lob(qtys: list[int], base_price_scaled: int = 1_000_000) -> np.ndarray:
    """Build (N,2) int64 array: col0=price_scaled (descending), col1=qty."""
    n = len(qtys)
    arr = np.zeros((n, 2), dtype=np.int64)
    for i, q in enumerate(qtys):
        arr[i, 0] = base_price_scaled - i * 100
        arr[i, 1] = q
    return arr


def _make_ask_lob(qtys: list[int], base_price_scaled: int = 1_000_100) -> np.ndarray:
    """Build (N,2) int64 array: col0=price_scaled (ascending), col1=qty."""
    n = len(qtys)
    arr = np.zeros((n, 2), dtype=np.int64)
    for i, q in enumerate(qtys):
        arr[i, 0] = base_price_scaled + i * 100
        arr[i, 1] = q
    return arr


# ── Manifest tests ────────────────────────────────────────────────────────────

def test_manifest_alpha_id() -> None:
    assert LobShapeAlpha().manifest.alpha_id == "lob_shape"


def test_manifest_tier_1() -> None:
    from research.registry.schemas import AlphaTier
    assert LobShapeAlpha().manifest.tier == AlphaTier.TIER_1


def test_manifest_rust_module() -> None:
    assert LobShapeAlpha().manifest.rust_module == "alpha_depth_slope"


def test_manifest_complexity_gate_a_acceptable() -> None:
    # Gate A only accepts O(1) or O(N)
    assert LobShapeAlpha().manifest.complexity in {"O(1)", "O(N)"}


def test_manifest_data_fields_complete() -> None:
    fields = set(LobShapeAlpha().manifest.data_fields)
    assert {"bids", "asks", "ofi_l1_ema8", "depth_imbalance_ema8_ppm"}.issubset(fields)


# ── _compute_slope tests ──────────────────────────────────────────────────────

def test_slope_flat_book_near_zero() -> None:
    """Uniform depth → log(qty+1) constant → slope ≈ 0."""
    lob = _make_bid_lob([1000, 1000, 1000, 1000, 1000])
    assert abs(_compute_slope(lob, 5)) < 1e-9


def test_slope_decreasing_depth_negative() -> None:
    """More qty near top (index 1) → log(qty) decreasing → negative slope."""
    lob = _make_bid_lob([1000, 800, 600, 400, 200])
    assert _compute_slope(lob, 5) < 0


def test_slope_increasing_depth_positive() -> None:
    """More qty deeper in book → positive slope."""
    lob = _make_bid_lob([200, 400, 600, 800, 1000])
    assert _compute_slope(lob, 5) > 0


def test_slope_single_level_returns_zero() -> None:
    assert _compute_slope(_make_bid_lob([500]), 10) == 0.0


def test_slope_empty_array_returns_zero() -> None:
    assert _compute_slope(np.zeros((0, 2), dtype=np.int64), 10) == 0.0


def test_slope_zero_qty_safe() -> None:
    """Zero quantities must not raise."""
    lob = _make_bid_lob([0, 0, 0])
    assert np.isfinite(_compute_slope(lob, 3))


def test_slope_n_levels_truncated() -> None:
    """Only first n_levels rows used."""
    # First 3 levels: flat → slope ≈ 0
    # Rows 4-10: strongly increasing (would give positive slope if used)
    qtys = [1000, 1000, 1000, 100, 200, 400, 800, 1600, 3200, 6400]
    lob = _make_bid_lob(qtys)
    slope_3 = _compute_slope(lob, 3)
    slope_10 = _compute_slope(lob, 10)
    assert abs(slope_3) < abs(slope_10)


# ── _sign_align tests ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("a, b, expected", [
    (100,  200,  1),
    (-50,  -20,  1),
    (100,  -50, -1),
    (-100,   50, -1),
    (0,    100,  0),
    (100,    0,  0),
    (0,      0,  0),
])
def test_sign_align_parametrized(a: int, b: int, expected: int) -> None:
    assert _sign_align(a, b) == expected


# ── LobShapeAlpha signal tests ────────────────────────────────────────────────

def test_update_returns_finite_float() -> None:
    alpha = LobShapeAlpha()
    bids = _make_bid_lob([1000, 800, 600])
    asks = _make_ask_lob([500, 600, 700])
    result = alpha.update(bids=bids, asks=asks, ofi_l1_ema8=100, depth_imbalance_ema8_ppm=50_000)
    assert isinstance(result, float)
    assert np.isfinite(result)


def test_update_deterministic() -> None:
    bids = _make_bid_lob([1000, 800, 600, 400])
    asks = _make_ask_lob([400, 600, 800, 1000])
    a1, a2 = LobShapeAlpha(), LobShapeAlpha()
    s1 = a1.update(bids=bids, asks=asks, ofi_l1_ema8=200, depth_imbalance_ema8_ppm=-50_000)
    s2 = a2.update(bids=bids.copy(), asks=asks.copy(), ofi_l1_ema8=200, depth_imbalance_ema8_ppm=-50_000)
    assert s1 == s2


def test_reset_clears_signal() -> None:
    alpha = LobShapeAlpha()
    alpha.update(bids=_make_bid_lob([1000, 800]), asks=_make_ask_lob([600, 800]),
                 ofi_l1_ema8=50, depth_imbalance_ema8_ppm=10_000)
    alpha.reset()
    assert alpha.get_signal() == 0.0


def test_steeper_ask_slope_positive_signal() -> None:
    """Steeper ask slope with neutral OFI → positive raw slope diff → positive signal."""
    bids = _make_bid_lob([1000, 1000, 1000, 1000, 1000])           # flat bid
    asks = _make_ask_lob([100, 500, 1000, 2000, 4000])             # strongly increasing ask
    alpha = LobShapeAlpha()
    alpha.update(bids=bids, asks=asks, ofi_l1_ema8=0, depth_imbalance_ema8_ppm=0)
    assert alpha.get_signal() > 0


def test_lambda_amplifies_aligned_signal() -> None:
    """With positive sign_align, higher lambda increases signal magnitude."""
    bids = _make_bid_lob([1000, 800, 600])
    asks = _make_ask_lob([400, 700, 1000])
    a_no = LobShapeAlpha(lambda_=0.0)
    a_with = LobShapeAlpha(lambda_=0.5)
    s_no = a_no.update(bids=bids, asks=asks, ofi_l1_ema8=100, depth_imbalance_ema8_ppm=50_000)
    s_with = a_with.update(bids=bids.copy(), asks=asks.copy(), ofi_l1_ema8=100, depth_imbalance_ema8_ppm=50_000)
    assert s_with > s_no


def test_update_positional_args() -> None:
    alpha = LobShapeAlpha()
    bids = _make_bid_lob([1000, 800])
    asks = _make_ask_lob([600, 800])
    result = alpha.update(bids, asks, 100, 50_000)
    assert isinstance(result, float)


def test_zero_volume_lob_no_crash() -> None:
    alpha = LobShapeAlpha()
    bids = np.zeros((5, 2), dtype=np.int64)
    asks = np.zeros((5, 2), dtype=np.int64)
    result = alpha.update(bids=bids, asks=asks, ofi_l1_ema8=0, depth_imbalance_ema8_ppm=0)
    assert np.isfinite(result)


def test_update_batch_returns_float64_array() -> None:
    alpha = LobShapeAlpha()
    n = 5
    arr = np.zeros(n, dtype=[
        ("bid_depth", "i8"), ("ask_depth", "i8"),
        ("ofi_l1_ema8", "i8"), ("depth_imbalance_ema8_ppm", "i8"),
    ])
    arr["bid_depth"] = [1000, 800, 600, 400, 200]
    arr["ask_depth"] = [200, 400, 600, 800, 1000]
    arr["ofi_l1_ema8"] = [10, -5, 0, 20, -10]
    arr["depth_imbalance_ema8_ppm"] = [50_000, -30_000, 0, 80_000, -20_000]
    result = alpha.update_batch(arr)
    assert result.shape == (n,)
    assert result.dtype == np.float64
    assert np.all(np.isfinite(result))


def test_update_batch_empty_array() -> None:
    alpha = LobShapeAlpha()
    arr = np.zeros(0, dtype=[("bid_depth", "i8"), ("ask_depth", "i8")])
    result = alpha.update_batch(arr)
    assert result.shape == (0,)
