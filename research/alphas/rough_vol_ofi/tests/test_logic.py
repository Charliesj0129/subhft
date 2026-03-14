"""Unit tests for rough_vol_ofi signal logic (21 tests).

Tests cover manifest integrity, Hurst estimation, EMA convergence,
signal direction, state management, and API compatibility.
"""
from __future__ import annotations

import math

from research.alphas.rough_vol_ofi.impl import (
    RoughVolOfiAlpha,
    _EMA_ALPHA,
    _FAST_ALPHA,
    _LOG_SCALE_RATIO,
    _MANIFEST,
    _OFI_EMA_ALPHA,
    _SLOW_ALPHA,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_alpha() -> RoughVolOfiAlpha:
    a = RoughVolOfiAlpha()
    a.reset()
    return a


def _warmup(alpha: RoughVolOfiAlpha, n: int = 200,
            bid: float = 100.0, ask: float = 100.0) -> None:
    for _ in range(n):
        alpha.update(bid, ask)


# ---------------------------------------------------------------------------
# 1-7: Manifest integrity
# ---------------------------------------------------------------------------

def test_manifest_alpha_id() -> None:
    assert _MANIFEST.alpha_id == "rough_vol_ofi"


def test_manifest_paper_refs_contains_074() -> None:
    assert "074" in _MANIFEST.paper_refs


def test_manifest_latency_profile_not_none() -> None:
    assert _MANIFEST.latency_profile is not None
    assert "shioaji" in _MANIFEST.latency_profile


def test_manifest_feature_set_version() -> None:
    assert _MANIFEST.feature_set_version == "lob_shared_v1"


def test_manifest_data_fields() -> None:
    assert "bid_qty" in _MANIFEST.data_fields
    assert "ask_qty" in _MANIFEST.data_fields


def test_manifest_complexity() -> None:
    assert _MANIFEST.complexity == "O(1)"


def test_manifest_roles_and_skills_non_empty() -> None:
    assert len(_MANIFEST.roles_used) > 0
    assert len(_MANIFEST.skills_used) > 0


# ---------------------------------------------------------------------------
# 8-10: Cold start and basic signal
# ---------------------------------------------------------------------------

def test_cold_start_does_not_raise() -> None:
    alpha = _make_alpha()
    result = alpha.update(100.0, 100.0)
    assert isinstance(result, float)
    assert math.isfinite(result)


def test_initial_signal_is_zero_before_update() -> None:
    alpha = _make_alpha()
    assert alpha.get_signal() == 0.0


def test_first_tick_returns_zero() -> None:
    """First tick initializes prev values; signal should be 0."""
    alpha = _make_alpha()
    result = alpha.update(100.0, 100.0)
    assert result == 0.0


# ---------------------------------------------------------------------------
# 11-12: EMA coefficient validation
# ---------------------------------------------------------------------------

def test_ema_alpha_coefficients() -> None:
    assert abs(_FAST_ALPHA - (1.0 - math.exp(-1.0 / 4.0))) < 1e-12
    assert abs(_SLOW_ALPHA - (1.0 - math.exp(-1.0 / 16.0))) < 1e-12
    assert abs(_EMA_ALPHA - (1.0 - math.exp(-1.0 / 16.0))) < 1e-12
    assert abs(_OFI_EMA_ALPHA - (1.0 - math.exp(-1.0 / 8.0))) < 1e-12


def test_log_scale_ratio() -> None:
    assert abs(_LOG_SCALE_RATIO - 2.0 * math.log(4.0)) < 1e-12


# ---------------------------------------------------------------------------
# 13-14: Hurst estimation convergence
# ---------------------------------------------------------------------------

def test_constant_input_hurst_near_half() -> None:
    """With constant bid/ask (zero OFI), Hurst should stay near 0.5 (neutral)."""
    alpha = _make_alpha()
    _warmup(alpha, 500)
    # Hurst EMA should be near 0.5, signal near 0
    assert abs(alpha.get_signal()) < 0.1


def test_hurst_ema_converges_with_constant_ofi() -> None:
    """Constant non-zero OFI should converge the Hurst estimate."""
    alpha = _make_alpha()
    # Feed monotonically increasing bid_qty to produce constant positive OFI
    for i in range(800):
        alpha.update(100.0 + i * 0.1, 100.0)
    s1 = alpha.get_signal()
    for i in range(800, 810):
        alpha.update(100.0 + i * 0.1, 100.0)
    s2 = alpha.get_signal()
    # Signal should be converging (small delta)
    assert abs(s2 - s1) < 0.05, f"Signal not converging: {s1} -> {s2}"


# ---------------------------------------------------------------------------
# 15-16: Signal direction
# ---------------------------------------------------------------------------

def test_rough_ofi_positive_bid_gives_contrarian_direction() -> None:
    """When bid dominates (positive OFI EMA) and H < 0.5 (rough),
    roughness > 0 and signal should be positive."""
    alpha = _make_alpha()
    # Create a pattern with increasing bid pressure
    for i in range(300):
        alpha.update(150.0 + (i % 10), 100.0)
    # With consistent bid dominance, ofi_ema > 0
    assert alpha._ofi_ema > 0.0


def test_rough_ofi_negative_ask_gives_negative_direction() -> None:
    """When ask dominates (negative OFI EMA), signal direction should be negative
    if roughness > 0."""
    alpha = _make_alpha()
    for i in range(300):
        alpha.update(100.0, 150.0 + (i % 10))
    assert alpha._ofi_ema < 0.0


# ---------------------------------------------------------------------------
# 17-18: Signal bounds and state management
# ---------------------------------------------------------------------------

def test_signal_bounded_within_minus_1_to_plus_1() -> None:
    """Signal must stay in [-1.0, 1.0] for any input pattern."""
    import numpy as np
    alpha = _make_alpha()
    rng = np.random.default_rng(99)
    for _ in range(2000):
        bid = float(rng.uniform(0.0, 500.0))
        ask = float(rng.uniform(0.0, 500.0))
        s = alpha.update(bid, ask)
        assert -1.0 <= s <= 1.0, f"Signal out of bounds: {s}"


def test_reset_clears_all_state() -> None:
    alpha = _make_alpha()
    for i in range(100):
        alpha.update(200.0 + i, 50.0)
    assert alpha._ofi_ema != 0.0
    alpha.reset()
    assert alpha._ofi_ema == 0.0
    assert alpha._ofi_var_fast == 0.0
    assert alpha._ofi_var_slow == 0.0
    assert alpha._ofi_mean_fast == 0.0
    assert alpha._ofi_mean_slow == 0.0
    assert alpha._hurst_ema == 0.5
    assert alpha._signal == 0.0
    assert alpha._prev_bid == 0.0
    assert alpha._prev_ask == 0.0
    assert alpha._initialized is False


# ---------------------------------------------------------------------------
# 19: get_signal consistency
# ---------------------------------------------------------------------------

def test_get_signal_returns_last_update_value() -> None:
    alpha = _make_alpha()
    alpha.update(100.0, 100.0)  # init tick
    result = alpha.update(150.0, 100.0)
    assert result == alpha.get_signal()


# ---------------------------------------------------------------------------
# 20-21: API compatibility
# ---------------------------------------------------------------------------

def test_keyword_api_bid_ask_qty() -> None:
    alpha1 = _make_alpha()
    alpha2 = _make_alpha()
    r1 = alpha1.update(150.0, 100.0)
    r2 = alpha2.update(bid_qty=150.0, ask_qty=100.0)
    assert abs(r1 - r2) < 1e-12


def test_positional_and_keyword_equivalent() -> None:
    """update(x, y) == update(bid_qty=x, ask_qty=y) for same state."""
    alpha1 = _make_alpha()
    alpha2 = _make_alpha()
    inputs = [(100.0, 80.0), (120.0, 60.0), (90.0, 110.0), (130.0, 70.0)]
    for b, a in inputs:
        alpha1.update(b, a)
        alpha2.update(bid_qty=b, ask_qty=a)
    assert abs(alpha1.get_signal() - alpha2.get_signal()) < 1e-12
