"""Gate B correctness tests for KyleLambdaAlpha (ref 001)."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.kyle_lambda.impl import (
    ALPHA_CLASS,
    KyleLambdaAlpha,
    _EMA_ALPHA_8,
    _EMA_ALPHA_16,
    _EPSILON,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert KyleLambdaAlpha().manifest.alpha_id == "kyle_lambda"


def test_manifest_tier_is_ensemble() -> None:
    from research.registry.schemas import AlphaTier

    assert KyleLambdaAlpha().manifest.tier == AlphaTier.ENSEMBLE


def test_manifest_paper_refs_includes_001() -> None:
    assert "001" in KyleLambdaAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = KyleLambdaAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields
    assert "mid_price" in fields


def test_manifest_latency_profile_set() -> None:
    assert KyleLambdaAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert KyleLambdaAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = KyleLambdaAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is KyleLambdaAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_first_tick_returns_zero() -> None:
    alpha = KyleLambdaAlpha()
    sig = alpha.update(100.0, 100.0, 35000.0)
    assert sig == 0.0


def test_signal_bounded() -> None:
    alpha = KyleLambdaAlpha()
    rng = np.random.default_rng(42)
    for _ in range(500):
        sig = alpha.update(
            rng.uniform(1, 1000),
            rng.uniform(1, 1000),
            rng.uniform(30000, 40000),
        )
        assert -2.0 <= sig <= 2.0


def test_constant_queues_signal_decays() -> None:
    alpha = KyleLambdaAlpha()
    alpha.update(100.0, 100.0, 35000.0)
    for _ in range(200):
        sig = alpha.update(100.0, 100.0, 35000.0)
    assert abs(sig) < 1e-4


def test_zero_inputs_safe() -> None:
    alpha = KyleLambdaAlpha()
    sig = alpha.update(0.0, 0.0, 0.0)
    assert isinstance(sig, float)
    sig2 = alpha.update(0.0, 0.0, 0.0)
    assert math.isfinite(sig2)


# ---------------------------------------------------------------------------
# Directional behaviour
# ---------------------------------------------------------------------------


def test_buying_with_price_increase_positive() -> None:
    """Bid increase + price increase -> positive lambda * positive OFI -> positive."""
    alpha = KyleLambdaAlpha()
    alpha.update(100.0, 100.0, 35000.0)
    bid = 100.0
    price = 35000.0
    for _ in range(50):
        bid += 5.0
        price += 1.0
        alpha.update(bid, 100.0, price)
    assert alpha.get_signal() > 0.0


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_keyword_args() -> None:
    alpha = KyleLambdaAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0, mid_price=35000.0)
    assert isinstance(sig, float)


def test_one_arg_raises() -> None:
    alpha = KyleLambdaAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_two_args_raises() -> None:
    alpha = KyleLambdaAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0, 100.0)


def test_reset_clears_state() -> None:
    alpha = KyleLambdaAlpha()
    alpha.update(100.0, 100.0, 35000.0)
    alpha.update(500.0, 50.0, 35005.0)
    alpha.reset()
    sig = alpha.update(100.0, 100.0, 35000.0)
    assert sig == 0.0


def test_get_signal_before_update() -> None:
    assert KyleLambdaAlpha().get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = KyleLambdaAlpha()
    assert isinstance(alpha, AlphaProtocol)


def test_slots_no_dict() -> None:
    alpha = KyleLambdaAlpha()
    assert not hasattr(alpha, "__dict__")
