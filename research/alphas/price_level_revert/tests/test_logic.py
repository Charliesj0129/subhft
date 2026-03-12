"""Gate B correctness tests for PriceLevelRevertAlpha."""
from __future__ import annotations

import math

import numpy as np
import pytest

from research.alphas.price_level_revert.impl import (
    ALPHA_CLASS,
    PriceLevelRevertAlpha,
    _EMA_ALPHA_16,
    _EMA_ALPHA_128,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert PriceLevelRevertAlpha().manifest.alpha_id == "price_level_revert"


def test_manifest_data_fields() -> None:
    fields = PriceLevelRevertAlpha().manifest.data_fields
    assert "mid_price_x2" in fields
    assert "spread_scaled" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D."""
    assert PriceLevelRevertAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert PriceLevelRevertAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = PriceLevelRevertAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is PriceLevelRevertAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_initial_signal_zero() -> None:
    """Before any update, signal should be zero."""
    alpha = PriceLevelRevertAlpha()
    assert alpha.get_signal() == 0.0


def test_first_update_zero_deviation() -> None:
    """First tick: EMA128 initializes to mid_price_x2, so deviation = 0, signal = 0."""
    alpha = PriceLevelRevertAlpha()
    sig = alpha.update(200000, 100)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_price_above_avg_negative_signal() -> None:
    """When price rises above the slow EMA, signal should be negative (fade deviation)."""
    alpha = PriceLevelRevertAlpha()
    # Warm up at baseline
    for _ in range(200):
        alpha.update(100000, 100)
    # Now price jumps up
    for _ in range(50):
        alpha.update(110000, 100)
    assert alpha.get_signal() < 0.0


def test_price_below_avg_positive_signal() -> None:
    """When price drops below the slow EMA, signal should be positive (fade deviation)."""
    alpha = PriceLevelRevertAlpha()
    # Warm up at baseline
    for _ in range(200):
        alpha.update(100000, 100)
    # Now price drops
    for _ in range(50):
        alpha.update(90000, 100)
    assert alpha.get_signal() > 0.0


def test_at_avg_zero_signal() -> None:
    """Constant price => deviation = 0 => signal converges to 0."""
    alpha = PriceLevelRevertAlpha()
    for _ in range(500):
        alpha.update(100000, 100)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-6)


def test_spread_normalization() -> None:
    """Wider spread should dampen the raw deviation before EMA."""
    alpha_narrow = PriceLevelRevertAlpha()
    alpha_wide = PriceLevelRevertAlpha()
    # Warm up both at same baseline
    for _ in range(200):
        alpha_narrow.update(100000, 10)
        alpha_wide.update(100000, 1000)
    # Same price jump
    for _ in range(50):
        alpha_narrow.update(110000, 10)
        alpha_wide.update(110000, 1000)
    # Narrow spread => larger normalized deviation => larger |signal|
    assert abs(alpha_narrow.get_signal()) > abs(alpha_wide.get_signal())


def test_convergence_constant_input() -> None:
    """With constant mid_price_x2 and spread, signal should converge to 0."""
    alpha = PriceLevelRevertAlpha()
    for _ in range(1000):
        alpha.update(50000, 50)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-8)


def test_reset_clears_state() -> None:
    """After reset, first update should return 0 (fresh initialization)."""
    alpha = PriceLevelRevertAlpha()
    alpha.update(200000, 100)
    alpha.update(300000, 100)
    alpha.reset()
    sig = alpha.update(100000, 50)
    # First tick after reset: EMA128 = mid_price_x2, deviation = 0
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_get_signal_returns_last() -> None:
    """get_signal() returns the cached signal from the most recent update()."""
    alpha = PriceLevelRevertAlpha()
    for _ in range(200):
        alpha.update(100000, 100)
    sig = alpha.update(120000, 100)
    assert alpha.get_signal() == sig


def test_kwargs_interface() -> None:
    """update() with keyword args should produce same result as positional."""
    a1 = PriceLevelRevertAlpha()
    a2 = PriceLevelRevertAlpha()
    s1 = a1.update(100000, 50)
    s2 = a2.update(mid_price_x2=100000, spread_scaled=50)
    assert s1 == pytest.approx(s2, abs=1e-12)


def test_positional_interface() -> None:
    """update() with 2 positional args works correctly."""
    alpha = PriceLevelRevertAlpha()
    sig = alpha.update(100000, 100)
    assert isinstance(sig, float)


def test_symmetric_deviation() -> None:
    """Symmetric positive/negative deviations should produce opposite signals."""
    a_up = PriceLevelRevertAlpha()
    a_down = PriceLevelRevertAlpha()
    base = 100000
    delta = 5000
    spread = 100
    # Warm up both at baseline
    for _ in range(300):
        a_up.update(base, spread)
        a_down.update(base, spread)
    # Apply symmetric deviations
    for _ in range(50):
        a_up.update(base + delta, spread)
        a_down.update(base - delta, spread)
    # Signals should be approximately opposite
    assert a_up.get_signal() < 0.0
    assert a_down.get_signal() > 0.0
    assert abs(a_up.get_signal() + a_down.get_signal()) < abs(a_up.get_signal()) * 0.2


def test_bounded_random_inputs() -> None:
    """Signal should remain finite for random inputs."""
    alpha = PriceLevelRevertAlpha()
    rng = np.random.default_rng(42)
    for _ in range(500):
        mid = float(rng.integers(50000, 150000))
        spread = float(rng.integers(1, 500))
        sig = alpha.update(mid, spread)
        assert math.isfinite(sig)


def test_wide_spread_dampens_signal() -> None:
    """Wider spread should dampen signal magnitude."""
    alpha = PriceLevelRevertAlpha()
    # Warm up, then shock
    for _ in range(200):
        alpha.update(100000, 500)
    for _ in range(30):
        alpha.update(110000, 500)
    wide_sig = abs(alpha.get_signal())

    alpha2 = PriceLevelRevertAlpha()
    for _ in range(200):
        alpha2.update(100000, 10)
    for _ in range(30):
        alpha2.update(110000, 10)
    narrow_sig = abs(alpha2.get_signal())

    assert narrow_sig > wide_sig


def test_narrow_spread_amplifies_signal() -> None:
    """Narrow spread should amplify signal magnitude (inverse of wide spread test)."""
    alpha_narrow = PriceLevelRevertAlpha()
    alpha_mid = PriceLevelRevertAlpha()
    for _ in range(200):
        alpha_narrow.update(100000, 1)
        alpha_mid.update(100000, 100)
    for _ in range(30):
        alpha_narrow.update(110000, 1)
        alpha_mid.update(110000, 100)
    assert abs(alpha_narrow.get_signal()) > abs(alpha_mid.get_signal())


# ---------------------------------------------------------------------------
# EMA correctness
# ---------------------------------------------------------------------------


def test_ema_alpha_values() -> None:
    """Verify EMA coefficients are correct."""
    assert _EMA_ALPHA_16 == pytest.approx(1.0 - math.exp(-1.0 / 16.0), abs=1e-12)
    assert _EMA_ALPHA_128 == pytest.approx(1.0 - math.exp(-1.0 / 128.0), abs=1e-12)


def test_ema_second_step_manual() -> None:
    """Manual check: second tick's EMA128 and deviation calculation."""
    alpha = PriceLevelRevertAlpha()
    mid1, spread = 100000.0, 100.0
    mid2 = 110000.0
    # Tick 1: EMA128 = mid1, deviation = 0, dev_ema16 = 0, signal = 0
    alpha.update(mid1, spread)
    # Tick 2: EMA128 += alpha128 * (mid2 - mid1)
    sig2 = alpha.update(mid2, spread)
    ema128_after = mid1 + _EMA_ALPHA_128 * (mid2 - mid1)
    dev = mid2 - ema128_after
    norm_dev = dev / spread
    # dev_ema16 was 0, so: dev_ema16 = 0 + alpha16 * (norm_dev - 0) = alpha16 * norm_dev
    expected_dev_ema16 = _EMA_ALPHA_16 * norm_dev
    expected_signal = -expected_dev_ema16
    assert sig2 == pytest.approx(expected_signal, abs=1e-9)


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = PriceLevelRevertAlpha()
    assert isinstance(alpha, AlphaProtocol)
