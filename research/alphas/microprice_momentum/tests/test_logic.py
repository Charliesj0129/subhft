"""Gate B correctness tests for MicropriceMomentumAlpha."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.microprice_momentum.impl import (
    _EMA_ALPHA_8,
    ALPHA_CLASS,
    MicropriceMomentumAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert MicropriceMomentumAlpha().manifest.alpha_id == "microprice_momentum"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert MicropriceMomentumAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_data_fields() -> None:
    fields = MicropriceMomentumAlpha().manifest.data_fields
    assert "microprice_x2" in fields
    assert "spread_scaled" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert MicropriceMomentumAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert MicropriceMomentumAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = MicropriceMomentumAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is MicropriceMomentumAlpha


# ---------------------------------------------------------------------------
# First update behavior
# ---------------------------------------------------------------------------


def test_first_update_zero() -> None:
    """First tick stores prev microprice, returns 0 (no delta yet)."""
    alpha = MicropriceMomentumAlpha()
    sig = alpha.update(200000, 100)
    assert sig == 0.0


def test_initial_zero() -> None:
    """get_signal() before any update is zero."""
    alpha = MicropriceMomentumAlpha()
    assert alpha.get_signal() == 0.0


def test_second_nonzero() -> None:
    """Second update with a different microprice must produce non-zero signal."""
    alpha = MicropriceMomentumAlpha()
    alpha.update(200000, 100)
    sig = alpha.update(200100, 100)
    assert sig != 0.0


# ---------------------------------------------------------------------------
# Signal direction
# ---------------------------------------------------------------------------


def test_rising_microprice_positive() -> None:
    """Consistently rising microprice should produce positive signal."""
    alpha = MicropriceMomentumAlpha()
    for i in range(20):
        alpha.update(200000 + i * 100, 100)
    assert alpha.get_signal() > 0.0


def test_falling_negative() -> None:
    """Consistently falling microprice should produce negative signal."""
    alpha = MicropriceMomentumAlpha()
    for i in range(20):
        alpha.update(200000 - i * 100, 100)
    assert alpha.get_signal() < 0.0


def test_stable_zero() -> None:
    """Stable microprice (no change) should converge signal toward zero."""
    alpha = MicropriceMomentumAlpha()
    # First a big move to create non-zero EMA
    alpha.update(200000, 100)
    alpha.update(201000, 100)
    # Then hold stable
    for _ in range(200):
        alpha.update(201000, 100)
    assert abs(alpha.get_signal()) < 0.01


def test_symmetric() -> None:
    """Rising and falling by same amount should produce symmetric signals."""
    a_up = MicropriceMomentumAlpha()
    a_down = MicropriceMomentumAlpha()
    a_up.update(200000, 100)
    a_down.update(200000, 100)
    sig_up = a_up.update(200100, 100)
    sig_down = a_down.update(199900, 100)
    assert sig_up == pytest.approx(-sig_down, abs=1e-9)


# ---------------------------------------------------------------------------
# Spread normalization
# ---------------------------------------------------------------------------


def test_spread_normalization() -> None:
    """Larger spread should attenuate the signal (same delta)."""
    a_narrow = MicropriceMomentumAlpha()
    a_wide = MicropriceMomentumAlpha()
    a_narrow.update(200000, 100)
    a_wide.update(200000, 1000)
    sig_narrow = a_narrow.update(200100, 100)
    sig_wide = a_wide.update(200100, 1000)
    assert abs(sig_narrow) > abs(sig_wide)


def test_spread_zero_uses_one() -> None:
    """spread_scaled=0 should use max(spread, 1) = 1, not divide by zero."""
    alpha = MicropriceMomentumAlpha()
    alpha.update(200000, 0)
    sig = alpha.update(200100, 0)
    # Should not raise; delta = 100 / 1 = 100
    assert sig != 0.0


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_convergence() -> None:
    """EMA should converge to the constant delta / spread given constant input."""
    alpha = MicropriceMomentumAlpha()
    micro_start = 200000
    step = 50
    spread = 100
    # Feed constant increments
    alpha.update(micro_start, spread)
    for i in range(1, 200):
        alpha.update(micro_start + i * step, spread)
    expected = step / spread  # constant delta / spread
    assert alpha.get_signal() == pytest.approx(expected, abs=0.01)


def test_ema_second_step() -> None:
    """Verify EMA calculation on the second update."""
    alpha = MicropriceMomentumAlpha()
    m1, m2, spread = 200000.0, 200100.0, 100.0
    alpha.update(m1, spread)
    sig = alpha.update(m2, spread)
    delta = (m2 - m1) / spread
    # First EMA step: ema was 0, so ema += alpha * (delta - 0) = alpha * delta
    expected = _EMA_ALPHA_8 * delta
    assert sig == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_kwargs() -> None:
    """update() accepts keyword arguments."""
    alpha = MicropriceMomentumAlpha()
    sig = alpha.update(microprice_x2=200000, spread_scaled=100)
    assert sig == 0.0  # first tick
    sig2 = alpha.update(microprice_x2=200100, spread_scaled=100)
    assert sig2 != 0.0


def test_positional() -> None:
    """update() accepts positional arguments."""
    alpha = MicropriceMomentumAlpha()
    sig = alpha.update(200000, 100)
    assert sig == 0.0


def test_one_arg_raises() -> None:
    """Single positional arg should raise ValueError."""
    alpha = MicropriceMomentumAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_reset() -> None:
    """reset() clears all state; next update acts as first tick."""
    alpha = MicropriceMomentumAlpha()
    alpha.update(200000, 100)
    alpha.update(200100, 100)
    assert alpha.get_signal() != 0.0
    alpha.reset()
    assert alpha.get_signal() == 0.0
    sig = alpha.update(200000, 100)
    assert sig == 0.0  # first tick after reset


def test_get_signal() -> None:
    """get_signal() returns last computed signal."""
    alpha = MicropriceMomentumAlpha()
    alpha.update(200000, 100)
    ret = alpha.update(200100, 100)
    assert alpha.get_signal() == ret


def test_bounded() -> None:
    """Signal should remain finite under random input."""
    import numpy as np

    alpha = MicropriceMomentumAlpha()
    rng = np.random.default_rng(42)
    micros = rng.integers(190000, 210000, 200)
    spreads = rng.integers(50, 500, 200)
    for m, s in zip(micros, spreads):
        sig = alpha.update(int(m), int(s))
        assert math.isfinite(sig)


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = MicropriceMomentumAlpha()
    assert isinstance(alpha, AlphaProtocol)
