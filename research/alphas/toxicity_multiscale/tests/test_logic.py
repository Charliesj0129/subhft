"""Gate B correctness tests for ToxicityMultiscaleAlpha (ref 129)."""
from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.toxicity_multiscale.impl import (
    ALPHA_CLASS,
    ToxicityMultiscaleAlpha,
    _EMA_ALPHA_8,
    _EMA_ALPHA_16,
    _EMA_ALPHA_64,
    _EPSILON,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert ToxicityMultiscaleAlpha().manifest.alpha_id == "toxicity_multiscale"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert ToxicityMultiscaleAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs_includes_129() -> None:
    assert "129" in ToxicityMultiscaleAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = ToxicityMultiscaleAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields
    assert "spread_scaled" in fields
    assert "mid_price" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert ToxicityMultiscaleAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert ToxicityMultiscaleAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = ToxicityMultiscaleAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is ToxicityMultiscaleAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_equal_queues_signal_zero() -> None:
    """Equal bid and ask queues -> QI=0 -> signal ~ 0."""
    alpha = ToxicityMultiscaleAlpha()
    # With QI=0, raw composite is 0 regardless of vol/spread.
    sig = alpha.update(100.0, 100.0, 50.0, 1000.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_bid_dominant_signal_positive() -> None:
    """bid > ask with price movement -> signal > 0."""
    alpha = ToxicityMultiscaleAlpha()
    for i in range(100):
        # Rising price with bid dominance and wide spread
        sig = alpha.update(500.0, 100.0, 200.0, 1000.0 + i * 10.0)
    assert alpha.get_signal() > 0.0


def test_ask_dominant_signal_negative() -> None:
    """ask > bid with price movement -> signal < 0."""
    alpha = ToxicityMultiscaleAlpha()
    for i in range(100):
        sig = alpha.update(100.0, 500.0, 200.0, 1000.0 + i * 10.0)
    assert alpha.get_signal() < 0.0


def test_signal_bounded() -> None:
    """Signal must stay in [-2, 2] under random fuzz."""
    alpha = ToxicityMultiscaleAlpha()
    rng = np.random.default_rng(42)
    bids = rng.uniform(0, 1000, 500)
    asks = rng.uniform(0, 1000, 500)
    spreads = rng.uniform(1, 500, 500)
    mids = np.cumsum(rng.uniform(-50, 50, 500)) + 10000.0
    for b, a, s, m in zip(bids, asks, spreads, mids):
        sig = alpha.update(b, a, s, m)
        assert -2.0 <= sig <= 2.0, f"signal {sig} out of bounds"


def test_zero_volatility_zero_signal() -> None:
    """Constant price -> vol=0 -> raw=0 -> signal ~ 0."""
    alpha = ToxicityMultiscaleAlpha()
    for _ in range(100):
        sig = alpha.update(500.0, 100.0, 100.0, 1000.0)
    # Vol converges to 0 because delta_mid is always 0 after first tick.
    # composite_ema8 should decay toward 0.
    assert abs(alpha.get_signal()) < 0.01


def test_ema_convergence() -> None:
    """Constant inputs should make EMA states converge."""
    alpha = ToxicityMultiscaleAlpha()
    for i in range(1000):
        # Alternating price to create steady volatility
        mid = 1000.0 + (i % 2) * 10.0
        alpha.update(300.0, 100.0, 50.0, mid)
    sig1 = alpha.get_signal()
    for i in range(100):
        mid = 1000.0 + (i % 2) * 10.0
        alpha.update(300.0, 100.0, 50.0, mid)
    sig2 = alpha.get_signal()
    # Should be very close after convergence
    assert sig1 == pytest.approx(sig2, abs=0.01)


def test_first_update_initializes() -> None:
    """First update should set _initialized and return a value."""
    alpha = ToxicityMultiscaleAlpha()
    sig = alpha.update(200.0, 100.0, 50.0, 1000.0)
    assert isinstance(sig, float)
    assert alpha._initialized is True


def test_update_accepts_keyword_args() -> None:
    alpha = ToxicityMultiscaleAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0, spread_scaled=50.0, mid_price=1000.0)
    assert isinstance(sig, float)


def test_update_wrong_arg_count_raises() -> None:
    alpha = ToxicityMultiscaleAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)
    with pytest.raises(ValueError):
        alpha.update(100.0, 200.0)
    with pytest.raises(ValueError):
        alpha.update(100.0, 200.0, 300.0)


def test_reset_clears_state() -> None:
    alpha = ToxicityMultiscaleAlpha()
    alpha.update(800.0, 100.0, 200.0, 5000.0)
    alpha.update(800.0, 100.0, 200.0, 5010.0)
    alpha.reset()
    # After reset, equal queues should give signal 0
    sig = alpha.update(300.0, 300.0, 50.0, 1000.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_get_signal_before_update_zero() -> None:
    alpha = ToxicityMultiscaleAlpha()
    assert alpha.get_signal() == 0.0


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = ToxicityMultiscaleAlpha()
    assert isinstance(alpha, AlphaProtocol)
