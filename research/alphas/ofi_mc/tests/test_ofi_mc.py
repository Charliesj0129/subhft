"""Comprehensive Gate B tests for ofi_mc alpha.

Tests behavioral correctness: OFI direction, edge cases, batch processing, reset.
Run with:
    uv run pytest research/alphas/ofi_mc/tests/ -v --no-cov
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.append(os.getcwd())

from research.alphas.ofi_mc.impl import OFIMCAlpha


# ── Manifest correctness ──────────────────────────────────────────────────────

def test_manifest_fields() -> None:
    alpha = OFIMCAlpha()
    assert alpha.manifest.alpha_id == "ofi_mc"
    assert alpha.manifest.rust_module == "alpha_ofi"
    assert alpha.manifest.complexity == "O(1)"


def test_manifest_data_fields_complete() -> None:
    fields = set(OFIMCAlpha().manifest.data_fields)
    required = {"bid_px", "bid_qty", "ask_px", "ask_qty", "trade_vol", "current_mid"}
    assert required.issubset(fields), f"Missing: {required - fields}"


# ── Functional: OFI direction ─────────────────────────────────────────────────

def test_first_tick_returns_zero() -> None:
    """First call returns 0.0 — no previous state to compute delta."""
    alpha = OFIMCAlpha()
    result = alpha.update(bid_px=100.0, bid_qty=10.0, ask_px=101.0, ask_qty=8.0,
                          trade_vol=0.0, current_mid=100.5)
    assert result == 0.0


def test_bid_lift_produces_positive_ofi() -> None:
    """Bid price improvement → b_flow = bid_qty → cumulative OFI grows positive."""
    alpha = OFIMCAlpha()
    alpha.update(bid_px=100.0, bid_qty=10.0, ask_px=101.0, ask_qty=8.0,
                 trade_vol=0.0, current_mid=100.5)
    result = alpha.update(bid_px=101.0, bid_qty=9.0, ask_px=102.0, ask_qty=7.0,
                          trade_vol=5.0, current_mid=101.5)
    assert result > 0.0, f"Expected positive OFI on bid lift, got {result}"


def test_ask_improvement_produces_negative_ofi() -> None:
    """Ask price drops (supply aggressively at lower ask) → negative OFI."""
    alpha = OFIMCAlpha()
    alpha.update(bid_px=100.0, bid_qty=10.0, ask_px=101.0, ask_qty=8.0,
                 trade_vol=0.0, current_mid=100.5)
    result = alpha.update(bid_px=100.0, bid_qty=10.0, ask_px=100.5, ask_qty=12.0,
                          trade_vol=5.0, current_mid=100.25)
    assert result < 0.0, f"Expected negative OFI on ask improvement, got {result}"


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_zero_volume_tick_accumulates_safely() -> None:
    """Ticks with zero trade_vol must not crash; OFI still updates."""
    alpha = OFIMCAlpha()
    alpha.update(bid_px=100.0, bid_qty=10.0, ask_px=101.0, ask_qty=8.0,
                 trade_vol=0.0, current_mid=100.5)
    result = alpha.update(bid_px=100.0, bid_qty=11.0, ask_px=101.0, ask_qty=8.0,
                          trade_vol=0.0, current_mid=100.5)
    assert np.isfinite(result)


def test_price_crossing_graceful() -> None:
    """Crossed book (bid > ask) must not raise."""
    alpha = OFIMCAlpha()
    alpha.update(bid_px=100.0, bid_qty=10.0, ask_px=101.0, ask_qty=8.0,
                 trade_vol=1.0, current_mid=100.5)
    result = alpha.update(bid_px=102.0, bid_qty=5.0, ask_px=101.5, ask_qty=5.0,
                          trade_vol=3.0, current_mid=101.75)
    assert np.isfinite(result)


def test_reset_clears_state() -> None:
    """After reset(), alpha behaves as if freshly instantiated."""
    alpha = OFIMCAlpha()
    for _ in range(5):
        alpha.update(bid_px=100.0, bid_qty=10.0, ask_px=101.0, ask_qty=8.0,
                     trade_vol=1.0, current_mid=100.5)
    alpha.reset()
    result = alpha.update(bid_px=100.0, bid_qty=10.0, ask_px=101.0, ask_qty=8.0,
                          trade_vol=1.0, current_mid=100.5)
    assert result == 0.0, "First tick after reset() should return 0.0"


# ── Batch processing ──────────────────────────────────────────────────────────

def test_update_batch_structured_array() -> None:
    """Batch update on structured numpy array returns correct-length float64 array."""
    alpha = OFIMCAlpha()
    n = 6
    arr = np.zeros(n, dtype=[
        ("bid_px", "f8"), ("bid_qty", "f8"),
        ("ask_px", "f8"), ("ask_qty", "f8"),
        ("trade_vol", "f8"), ("current_mid", "f8"),
    ])
    arr["bid_px"] = [100.0, 100.0, 101.0, 101.0, 100.5, 100.5]
    arr["ask_px"] = [101.0, 101.0, 102.0, 102.0, 101.5, 101.5]
    arr["bid_qty"] = [10, 11, 9, 8, 10, 12]
    arr["ask_qty"] = [8, 8, 7, 6, 8, 8]
    arr["trade_vol"] = [0, 1, 2, 1, 3, 2]
    arr["current_mid"] = [100.5] * n
    result = alpha.update_batch(arr)
    assert result.shape == (n,)
    assert result.dtype == np.float64
    assert np.all(np.isfinite(result))


def test_update_batch_empty_array() -> None:
    """Empty structured array returns empty float64 array."""
    alpha = OFIMCAlpha()
    arr = np.zeros(0, dtype=[("bid_px", "f8"), ("ask_px", "f8"),
                              ("bid_qty", "f8"), ("ask_qty", "f8")])
    result = alpha.update_batch(arr)
    assert result.shape == (0,)


def test_update_batch_alias_fields() -> None:
    """Batch path handles best_bid/best_ask alias column names."""
    alpha = OFIMCAlpha()
    arr = np.zeros(3, dtype=[
        ("best_bid", "f8"), ("best_ask", "f8"),
        ("bid_depth", "f8"), ("ask_depth", "f8"),
        ("trade_vol", "f8"), ("current_mid", "f8"),
    ])
    arr["best_bid"] = [100.0, 100.0, 101.0]
    arr["best_ask"] = [101.0, 101.0, 102.0]
    arr["bid_depth"] = [10, 11, 9]
    arr["ask_depth"] = [8, 8, 7]
    arr["trade_vol"] = [1, 2, 3]
    arr["current_mid"] = [100.5, 100.5, 101.5]
    result = alpha.update_batch(arr)
    assert result.shape == (3,)
    assert np.all(np.isfinite(result))
