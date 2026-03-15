"""Gate B correctness tests for SpreadAdverseRatioAlpha (ref 131)."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.spread_adverse_ratio.impl import (
    ALPHA_CLASS,
    SpreadAdverseRatioAlpha,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert SpreadAdverseRatioAlpha().manifest.alpha_id == "spread_adverse_ratio"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert SpreadAdverseRatioAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs_includes_131() -> None:
    assert "131" in SpreadAdverseRatioAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = SpreadAdverseRatioAlpha().manifest.data_fields
    assert "spread_scaled" in fields
    assert "mid_price" in fields
    assert "volume" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert SpreadAdverseRatioAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert SpreadAdverseRatioAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = SpreadAdverseRatioAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is SpreadAdverseRatioAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_zero_spread_signal_zero() -> None:
    """spread_scaled=0 -> signal=0 (no spread to decompose)."""
    alpha = SpreadAdverseRatioAlpha()
    sig = alpha.update(0.0, 100_0000, 500.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_high_spread_low_vol_signal_high() -> None:
    """Large spread with no price movement -> ratio near 1."""
    alpha = SpreadAdverseRatioAlpha()
    # Feed constant mid_price (no volatility) with large spread.
    for _ in range(50):
        sig = alpha.update(1000.0, 100_0000, 100.0)
    # With zero delta_mid after first tick, vol_proxy_ema decays to 0,
    # so adverse_component ~ spread -> signal ~ 1.
    assert sig > 0.9


def test_high_vol_low_spread_signal_low() -> None:
    """Large volatility relative to spread -> ratio near 0."""
    alpha = SpreadAdverseRatioAlpha()
    # Feed alternating mid_price with small spread to pump vol_proxy.
    for i in range(100):
        mid = 100_0000 + (5000 if i % 2 == 0 else -5000)
        sig = alpha.update(10.0, mid, 10000.0)
    # vol_component should dominate -> adverse_component <= 0 -> signal ~ 0.
    assert sig < 0.1


def test_signal_bounded_0_1() -> None:
    """Signal must stay in [0, 1] for random inputs."""
    alpha = SpreadAdverseRatioAlpha()
    rng = np.random.default_rng(42)
    spreads = rng.uniform(0, 2000, 300)
    mids = rng.uniform(90_0000, 110_0000, 300)
    volumes = rng.uniform(0, 5000, 300)
    for s, m, v in zip(spreads, mids, volumes):
        sig = alpha.update(float(s), float(m), float(v))
        assert 0.0 <= sig <= 1.0, f"signal {sig} out of [0,1]"


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_convergence() -> None:
    """Constant inputs should converge the EMA."""
    alpha = SpreadAdverseRatioAlpha()
    # First tick initializes; subsequent ticks have delta_mid=0.
    alpha.update(500.0, 100_0000, 100.0)
    signals = []
    for _ in range(100):
        sig = alpha.update(500.0, 100_0000, 100.0)
        signals.append(sig)
    # Last 10 should be nearly identical (EMA converged).
    assert max(signals[-10:]) - min(signals[-10:]) < 1e-6


def test_first_update_initializes() -> None:
    """First update should set _initialized and produce a valid signal."""
    alpha = SpreadAdverseRatioAlpha()
    assert not alpha._initialized
    sig = alpha.update(100.0, 100_0000, 50.0)
    assert alpha._initialized
    assert isinstance(sig, float)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = SpreadAdverseRatioAlpha()
    sig = alpha.update(spread_scaled=100.0, mid_price=100_0000, volume=50.0)
    assert isinstance(sig, float)
    assert 0.0 <= sig <= 1.0


def test_update_wrong_arg_count_raises() -> None:
    alpha = SpreadAdverseRatioAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0, 200.0)  # only 2 positional — need 3


def test_reset_clears_state() -> None:
    alpha = SpreadAdverseRatioAlpha()
    alpha.update(500.0, 100_0000, 200.0)
    alpha.update(500.0, 101_0000, 200.0)
    alpha.reset()
    assert alpha._signal == 0.0
    assert alpha._vol_proxy_ema == 0.0
    assert not alpha._initialized
    # After reset, first update should behave like fresh instance.
    sig = alpha.update(0.0, 100_0000, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_get_signal_before_update_zero() -> None:
    alpha = SpreadAdverseRatioAlpha()
    assert alpha.get_signal() == 0.0


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = SpreadAdverseRatioAlpha()
    assert isinstance(alpha, AlphaProtocol)
