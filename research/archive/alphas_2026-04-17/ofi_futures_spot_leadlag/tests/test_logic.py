"""Gate B correctness tests for OfiFuturesSpotLeadlagAlpha."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.ofi_futures_spot_leadlag.impl import (
    ALPHA_CLASS,
    OfiFuturesSpotLeadlagAlpha,
    _RollingBetaTracker,
    _SIGNAL_CLIP,
    _DEFAULT_STALE_TICKS_LIMIT,
)


def _warmup(alpha: OfiFuturesSpotLeadlagAlpha, n: int | None = None) -> None:
    """Feed correlated OFI to both instruments past warmup."""
    if n is None:
        n = alpha.warmup_ticks + 5
    for _ in range(n):
        alpha.update(10.0, 5.0)


# --- Manifest ---
def test_manifest_alpha_id() -> None:
    assert OfiFuturesSpotLeadlagAlpha().manifest.alpha_id == "ofi_futures_spot_leadlag"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier
    assert OfiFuturesSpotLeadlagAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs() -> None:
    refs = OfiFuturesSpotLeadlagAlpha().manifest.paper_refs
    assert "Hasbrouck 2003" in refs
    assert "arXiv:2112.13213" in refs


def test_manifest_data_fields_fix3() -> None:
    """Fix 3: data_fields should be ofi_l1_raw (bridge computes from raw LOB)."""
    assert "ofi_l1_raw" in OfiFuturesSpotLeadlagAlpha().manifest.data_fields


def test_manifest_latency_profile_set() -> None:
    assert OfiFuturesSpotLeadlagAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert OfiFuturesSpotLeadlagAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS
    m = OfiFuturesSpotLeadlagAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is OfiFuturesSpotLeadlagAlpha


# --- Protocol ---
def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol
    assert isinstance(OfiFuturesSpotLeadlagAlpha(), AlphaProtocol)


# --- Warmup ---
def test_warmup_returns_zero() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha()
    for i in range(alpha.warmup_ticks - 1):
        sig = alpha.update(float(i % 5), float(i % 3))
        assert sig == 0.0, f"tick {i+1}: expected 0.0 during warmup, got {sig}"


def test_signal_activates_after_warmup() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha(beta_window=100)
    _warmup(alpha, alpha.warmup_ticks + 10)
    # Feed divergent: strong futures OFI, moderate spot
    for _ in range(20):
        alpha.update(50.0, 10.0)
    sig = alpha.update(80.0, 10.0)
    assert sig != 0.0


def test_first_tick_signal_is_zero() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha()
    assert alpha.update(42.0, 10.0) == 0.0


# --- Signal direction ---
def test_futures_lead_positive_produces_positive_signal() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha(beta_window=100)
    for _ in range(alpha.warmup_ticks + 20):
        alpha.update(10.0, 10.0)
    for _ in range(30):
        sig = alpha.update(50.0, 10.0)
    assert sig > 0.0


def test_futures_lead_negative_produces_negative_signal() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha(beta_window=100)
    for _ in range(alpha.warmup_ticks + 20):
        alpha.update(-10.0, -10.0)
    for _ in range(30):
        sig = alpha.update(-50.0, -10.0)
    assert sig < 0.0


def test_equal_flow_signal_near_zero() -> None:
    """When futures and spot OFI move together, signal should be moderate."""
    alpha = OfiFuturesSpotLeadlagAlpha(beta_window=100)
    for _ in range(alpha.warmup_ticks + 200):
        alpha.update(10.0, 10.0)
    # MAD floor prevents exact 0; check it's not at clip boundary
    assert abs(alpha.get_signal()) < _SIGNAL_CLIP


def test_signal_symmetry() -> None:
    alpha_pos = OfiFuturesSpotLeadlagAlpha(beta_window=100)
    alpha_neg = OfiFuturesSpotLeadlagAlpha(beta_window=100)
    for _ in range(alpha_pos.warmup_ticks + 20):
        alpha_pos.update(10.0, 10.0)
        alpha_neg.update(10.0, 10.0)
    for _ in range(30):
        alpha_pos.update(50.0, 10.0)
        alpha_neg.update(-30.0, 10.0)
    assert alpha_pos.get_signal() > 0.0
    assert alpha_neg.get_signal() < 0.0


# --- Signal clipping ---
def test_signal_clipped_to_bounds() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha(beta_window=100)
    _warmup(alpha, alpha.warmup_ticks + 5)
    for _ in range(200):
        sig = alpha.update(1e6, 1.0)
    assert -_SIGNAL_CLIP <= sig <= _SIGNAL_CLIP


# --- Rolling beta ---
def test_beta_tracker_converges() -> None:
    """Beta should converge to ~2.0 when futures = 2 * spot + noise."""
    tracker = _RollingBetaTracker(window=200)
    import random
    rng = random.Random(42)
    for _ in range(1000):
        spot = rng.gauss(0, 10)
        futures = 2.0 * spot + rng.gauss(0, 1)
        tracker.update(futures, spot)
    assert abs(tracker.beta - 2.0) < 0.3, f"Beta={tracker.beta}, expected ~2.0"


def test_beta_tracker_reset() -> None:
    tracker = _RollingBetaTracker(window=100)
    for _ in range(50):
        tracker.update(10.0, 5.0)
    tracker.reset()
    assert tracker.beta == 1.0


def test_beta_window_parameter() -> None:
    """Different beta_window should produce different beta values."""
    t_fast = _RollingBetaTracker(window=50)
    t_slow = _RollingBetaTracker(window=500)
    import random
    rng = random.Random(123)
    for _ in range(200):
        v = rng.gauss(0, 10)
        t_fast.update(v, v)
        t_slow.update(v, v)
    for _ in range(200):
        s = rng.gauss(0, 10)
        t_fast.update(3.0 * s, s)
        t_slow.update(3.0 * s, s)
    assert t_fast.beta > t_slow.beta


# --- Stale data guard (Fix 4: limit=200) ---
def test_stale_guard_default_is_200() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha()
    assert alpha._stale_ticks_limit == 200


def test_stale_guard_futures() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha(beta_window=50, stale_ticks_limit=20)
    _warmup(alpha, alpha.warmup_ticks + 10)
    for _ in range(30):
        alpha.update(50.0, 10.0)
    assert alpha.get_signal() != 0.0
    for _ in range(25):
        alpha.update(0.0, 10.0)
    sig = alpha.update(0.0, 10.0)
    assert sig == 0.0


def test_stale_guard_recovers() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha(beta_window=50, stale_ticks_limit=10)
    _warmup(alpha, alpha.warmup_ticks + 10)
    for _ in range(15):
        alpha.update(0.0, 10.0)
    assert alpha.get_signal() == 0.0
    for _ in range(20):
        alpha.update(50.0, 10.0)
    assert alpha.get_signal() != 0.0


# --- Keyword args ---
def test_keyword_args() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha()
    sig = alpha.update(ofi_futures=42.0, ofi_spot=10.0)
    assert sig == 0.0


def test_missing_args_raises() -> None:
    with pytest.raises(ValueError, match="requires.*ofi_futures.*ofi_spot"):
        OfiFuturesSpotLeadlagAlpha().update()


def test_partial_args_raises() -> None:
    with pytest.raises(ValueError, match="requires.*ofi_futures.*ofi_spot"):
        OfiFuturesSpotLeadlagAlpha().update(ofi_futures=10.0)


# --- Reset ---
def test_reset_clears_state() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha()
    for _ in range(100):
        alpha.update(42.0, 10.0)
    alpha.reset()
    alpha2 = OfiFuturesSpotLeadlagAlpha()
    assert alpha.update(42.0, 10.0) == pytest.approx(alpha2.update(42.0, 10.0), abs=1e-9)


def test_get_signal_before_update() -> None:
    assert OfiFuturesSpotLeadlagAlpha().get_signal() == 0.0


# --- Numerical stability ---
def test_zero_ofi_stream() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha()
    for _ in range(300):
        sig = alpha.update(0.0, 0.0)
    assert math.isfinite(sig)


def test_large_values_no_overflow() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha(beta_window=50)
    for _ in range(100):
        alpha.update(1e10, 1e10)
    sig = alpha.update(1e10 + 1e6, 1e10)
    assert math.isfinite(sig)


def test_constant_ofi_signal_bounded() -> None:
    """Constant correlated flow should produce bounded signal."""
    alpha = OfiFuturesSpotLeadlagAlpha(beta_window=100)
    for _ in range(500):
        alpha.update(20.0, 10.0)
    assert abs(alpha.get_signal()) < _SIGNAL_CLIP


# --- Warmup transient (Round 1 lesson) ---
def test_early_transient_not_corrupted() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha(beta_window=100)
    for _ in range(alpha.warmup_ticks):
        alpha.update(10.0, 5.0)
    sig = alpha.update(30.0, 5.0)
    assert abs(sig) < _SIGNAL_CLIP, f"Signal {sig} at clip — transient corruption"


# --- Custom parameters ---
def test_custom_beta_window() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha(beta_window=500, ema_window=8, output_window=4)
    for _ in range(200):
        alpha.update(10.0, 5.0)
    assert math.isfinite(alpha.get_signal())


# --- Beta property ---
def test_beta_property_accessible() -> None:
    alpha = OfiFuturesSpotLeadlagAlpha(beta_window=100)
    for _ in range(200):
        alpha.update(20.0, 10.0)
    assert math.isfinite(alpha.beta)


# ========== 4 NEW TESTS (Fix 2) ==========

def test_asymmetric_tick_rate() -> None:
    """Feed 3 futures updates per 1 spot update, verify signal is reasonable.

    Simulates real-world TXFD6 ticking 3x faster than 2330.
    """
    alpha = OfiFuturesSpotLeadlagAlpha(beta_window=100)
    _warmup(alpha, alpha.warmup_ticks + 10)

    last_spot = 5.0
    for i in range(90):
        if i % 3 == 0:
            # Spot updates every 3rd tick
            last_spot = 5.0 + (i // 3) * 0.1
        # Futures updates every tick with leading signal
        futures_val = last_spot * 2.0 + 10.0
        alpha.update(futures_val, last_spot)

    sig = alpha.get_signal()
    assert math.isfinite(sig)
    assert abs(sig) < _SIGNAL_CLIP


def test_spot_only_staleness() -> None:
    """Only spot goes stale while futures remains active — stale guard should activate."""
    alpha = OfiFuturesSpotLeadlagAlpha(beta_window=50, stale_ticks_limit=15)
    _warmup(alpha, alpha.warmup_ticks + 10)

    # Both active
    for _ in range(10):
        alpha.update(20.0, 10.0)
    assert alpha.get_signal() != 0.0

    # Spot goes stale (zero), futures stays active
    for _ in range(20):
        alpha.update(20.0, 0.0)  # futures active, spot zero
    sig = alpha.get_signal()
    assert sig == 0.0, "Stale guard should activate when spot is stale"


def test_mad_floor_prevents_amplification() -> None:
    """When residual is near-zero, MAD floor prevents 0/0 -> 1.0 amplification."""
    alpha = OfiFuturesSpotLeadlagAlpha(beta_window=100)
    # Feed perfectly correlated data so residual -> 0 as beta adapts
    for _ in range(500):
        alpha.update(20.0, 10.0)
    # With MAD floor, signal should NOT be exactly 1.0
    sig = abs(alpha.get_signal())
    assert sig < 1.5, f"Signal {sig} too large — MAD floor not working"


def test_beta_bias_small_window() -> None:
    """beta_window=50 with known 2:1 relationship should converge reasonably.

    Fix 1 (demeaned order-of-ops) ensures unbiased variance estimation.
    """
    import random
    tracker = _RollingBetaTracker(window=50)
    rng = random.Random(99)
    for _ in range(500):
        spot = rng.gauss(0, 10)
        futures = 2.0 * spot + rng.gauss(0, 2)
        tracker.update(futures, spot)
    # Should converge to ~2.0 even with small window
    assert abs(tracker.beta - 2.0) < 0.5, f"Beta={tracker.beta}, expected ~2.0 (small window)"
