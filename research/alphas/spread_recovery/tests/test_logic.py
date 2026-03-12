"""Gate B correctness tests for SpreadRecoveryAlpha."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.spread_recovery.impl import (
    _EMA_ALPHA_16,
    _EMA_ALPHA_32,
    _PEAK_DECAY,
    ALPHA_CLASS,
    SpreadRecoveryAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert SpreadRecoveryAlpha().manifest.alpha_id == "spread_recovery"


def test_manifest_data_fields() -> None:
    fields = SpreadRecoveryAlpha().manifest.data_fields
    assert "spread_scaled" in fields


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert SpreadRecoveryAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_latency_profile_set() -> None:
    assert SpreadRecoveryAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert SpreadRecoveryAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = SpreadRecoveryAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is SpreadRecoveryAlpha


# ---------------------------------------------------------------------------
# Signal basics
# ---------------------------------------------------------------------------


def test_initial_signal_zero() -> None:
    """First update returns 0 (no prior state to compute delta)."""
    alpha = SpreadRecoveryAlpha()
    sig = alpha.update(1000)
    assert sig == 0.0


def test_get_signal_before_update_is_zero() -> None:
    alpha = SpreadRecoveryAlpha()
    assert alpha.get_signal() == 0.0


def test_widening_then_narrowing_positive() -> None:
    """Spread widens then narrows → delta_spread < 0 → recovery_raw > 0 → signal > 0."""
    alpha = SpreadRecoveryAlpha()
    # Warm up at baseline
    for _ in range(50):
        alpha.update(100)
    # Widen spread
    for _ in range(5):
        alpha.update(200)
    # Narrow spread (recovery)
    for _ in range(5):
        sig = alpha.update(100)
    assert sig > 0.0, "Signal should be positive after spread recovery"


def test_sustained_wide_negative() -> None:
    """Sustained widening → delta_spread > 0 → recovery_raw < 0 → signal < 0."""
    alpha = SpreadRecoveryAlpha()
    # Warm up at baseline
    for _ in range(50):
        alpha.update(100)
    # Continuously widen
    for i in range(20):
        sig = alpha.update(100 + (i + 1) * 10)
    assert sig < 0.0, "Signal should be negative during sustained widening"


def test_stable_spread_signal_near_zero() -> None:
    """Constant spread → delta = 0 → signal decays toward 0."""
    alpha = SpreadRecoveryAlpha()
    for _ in range(200):
        alpha.update(500)
    assert abs(alpha.get_signal()) < 1e-6


def test_reset_clears_state() -> None:
    alpha = SpreadRecoveryAlpha()
    alpha.update(1000)
    alpha.update(2000)
    alpha.reset()
    sig = alpha.update(500)
    assert sig == 0.0, "After reset, first update should return 0"


def test_get_signal_matches_update() -> None:
    alpha = SpreadRecoveryAlpha()
    alpha.update(100)
    ret = alpha.update(200)
    assert ret == alpha.get_signal()


def test_update_kwargs() -> None:
    alpha = SpreadRecoveryAlpha()
    sig = alpha.update(spread_scaled=1000)
    assert sig == 0.0


def test_update_positional() -> None:
    alpha = SpreadRecoveryAlpha()
    sig = alpha.update(1000)
    assert sig == 0.0


def test_convergence_constant_input() -> None:
    """With constant input, signal should converge to 0 (no delta)."""
    alpha = SpreadRecoveryAlpha()
    for _ in range(500):
        alpha.update(300)
    assert abs(alpha.get_signal()) < 1e-8


def test_peak_decay_over_time() -> None:
    """Peak dev decays when no new extremes appear."""
    alpha = SpreadRecoveryAlpha()
    # Warm up
    for _ in range(50):
        alpha.update(100)
    # Big spike
    alpha.update(500)
    peak_after_spike = alpha._peak_dev
    # Return to normal
    for _ in range(100):
        alpha.update(100)
    assert alpha._peak_dev < peak_after_spike, "Peak dev should decay over time"


def test_impulse_recovery_response() -> None:
    """Single-tick spike then immediate return should produce positive signal."""
    alpha = SpreadRecoveryAlpha()
    for _ in range(50):
        alpha.update(100)
    # Spike up
    alpha.update(300)
    # Immediate return
    sig = alpha.update(100)
    assert sig > 0.0, "Impulse recovery should produce positive signal"


def test_signal_bounded() -> None:
    """Signal should remain in a reasonable range for typical inputs."""
    import numpy as np

    alpha = SpreadRecoveryAlpha()
    rng = np.random.default_rng(42)
    spreads = rng.integers(50, 500, 500)
    for s in spreads:
        sig = alpha.update(int(s))
        # Signal is normalized by peak_dev, so should be bounded
        assert -10.0 <= sig <= 10.0, f"Signal {sig} out of reasonable range"


# ---------------------------------------------------------------------------
# EMA constant sanity
# ---------------------------------------------------------------------------


def test_ema_alpha_16_value() -> None:
    expected = 1.0 - math.exp(-1.0 / 16.0)
    assert _EMA_ALPHA_16 == pytest.approx(expected, abs=1e-12)


def test_ema_alpha_32_value() -> None:
    expected = 1.0 - math.exp(-1.0 / 32.0)
    assert _EMA_ALPHA_32 == pytest.approx(expected, abs=1e-12)


def test_peak_decay_value() -> None:
    assert _PEAK_DECAY == 0.99


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = SpreadRecoveryAlpha()
    assert isinstance(alpha, AlphaProtocol)
