"""Gate B correctness tests for VpinBvcAlpha (ref 134)."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.vpin.impl import ALPHA_CLASS, VpinBvcAlpha


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert VpinBvcAlpha().manifest.alpha_id == "vpin_bvc"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert VpinBvcAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs_includes_134() -> None:
    assert "134" in VpinBvcAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = VpinBvcAlpha().manifest.data_fields
    assert "mid_price" in fields
    assert "volume" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert VpinBvcAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert VpinBvcAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = VpinBvcAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is VpinBvcAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_zero_volume_signal_zero() -> None:
    """update(100.0, 0.0) should not crash and signal stays 0."""
    alpha = VpinBvcAlpha()
    sig = alpha.update(100.0, 0.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_constant_price_vpin_low() -> None:
    """Same price every tick -> buy_frac ~ 0.5 -> VPIN ~ 0."""
    alpha = VpinBvcAlpha(n_buckets=10, bucket_size=100.0)
    for _ in range(200):
        alpha.update(100.0, 50.0)
    assert alpha.get_signal() < 0.05


def test_rising_price_vpin_nonzero() -> None:
    """Consistently rising price -> buy > sell -> VPIN > 0."""
    alpha = VpinBvcAlpha(n_buckets=10, bucket_size=100.0)
    for i in range(500):
        alpha.update(100.0 + i * 0.1, 50.0)
    assert alpha.get_signal() > 0.0


def test_signal_bounded_0_1() -> None:
    """Signal must stay in [0, 1] at all times."""
    alpha = VpinBvcAlpha(n_buckets=10, bucket_size=50.0)
    rng = np.random.default_rng(42)
    prices = np.cumsum(rng.standard_normal(500)) + 100.0
    volumes = rng.uniform(1, 100, 500)
    for p, v in zip(prices, volumes):
        sig = alpha.update(float(p), float(v))
        assert 0.0 <= sig <= 1.0


def test_bucket_fills_after_sufficient_volume() -> None:
    """Bucket rotation occurs after accumulating bucket_size volume."""
    alpha = VpinBvcAlpha(n_buckets=5, bucket_size=100.0)
    # Feed 100 volume in one tick — should fill exactly one bucket
    alpha.update(100.0, 100.0)
    assert alpha._n_filled == 1


def test_first_update_initializes() -> None:
    """VPIN = 0 until first bucket fills."""
    alpha = VpinBvcAlpha(n_buckets=5, bucket_size=1000.0)
    sig = alpha.update(100.0, 10.0)
    assert sig == 0.0
    assert alpha._n_filled == 0


def test_update_accepts_keyword_args() -> None:
    alpha = VpinBvcAlpha(n_buckets=5, bucket_size=100.0)
    sig = alpha.update(mid_price=100.0, volume=200.0)
    # Should fill at least one bucket and not crash
    assert isinstance(sig, float)


def test_update_one_arg_raises() -> None:
    alpha = VpinBvcAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_reset_clears_state() -> None:
    alpha = VpinBvcAlpha(n_buckets=5, bucket_size=100.0)
    # Fill some buckets
    for i in range(50):
        alpha.update(100.0 + i, 50.0)
    assert alpha.get_signal() > 0.0
    alpha.reset()
    assert alpha.get_signal() == 0.0
    assert alpha._n_filled == 0
    assert alpha._initialized is False


def test_get_signal_before_update_is_zero() -> None:
    alpha = VpinBvcAlpha()
    assert alpha.get_signal() == 0.0


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = VpinBvcAlpha()
    assert isinstance(alpha, AlphaProtocol)


# ---------------------------------------------------------------------------
# VPIN-specific behaviour
# ---------------------------------------------------------------------------


def test_vpin_increases_with_directional_flow() -> None:
    """Strongly directional flow should yield higher VPIN than balanced flow."""
    alpha_dir = VpinBvcAlpha(n_buckets=10, bucket_size=100.0)
    alpha_bal = VpinBvcAlpha(n_buckets=10, bucket_size=100.0)

    # Directional: consistently rising price
    for i in range(300):
        alpha_dir.update(100.0 + i * 0.5, 50.0)
    # Balanced: price oscillates around 100
    for i in range(300):
        alpha_bal.update(100.0 + (-1) ** i * 0.01, 50.0)

    assert alpha_dir.get_signal() > alpha_bal.get_signal()


def test_ring_buffer_wraps() -> None:
    """After more than n_buckets fills, the ring buffer wraps correctly."""
    n = 5
    alpha = VpinBvcAlpha(n_buckets=n, bucket_size=100.0)
    for i in range(200):
        alpha.update(100.0 + i * 0.1, 50.0)
    assert alpha._n_filled == n
    # bucket_idx should have wrapped
    assert 0 <= alpha._bucket_idx < n
