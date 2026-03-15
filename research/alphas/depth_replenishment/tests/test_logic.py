"""Gate B correctness tests for DepthReplenishmentAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.depth_replenishment.impl import (
    ALPHA_CLASS,
    DepthReplenishmentAlpha,
    _EMA_ALPHA,
    _sign,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert DepthReplenishmentAlpha().manifest.alpha_id == "depth_replenishment"


def test_manifest_data_fields() -> None:
    fields = DepthReplenishmentAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert DepthReplenishmentAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert DepthReplenishmentAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = DepthReplenishmentAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is DepthReplenishmentAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_first_update_returns_zero() -> None:
    """First tick has no prior depth — signal must be 0."""
    alpha = DepthReplenishmentAlpha()
    sig = alpha.update(100.0, 100.0)
    assert sig == 0.0


def test_depth_increase_bid_dominant_positive() -> None:
    """Depth grows + bid > ask → positive signal."""
    alpha = DepthReplenishmentAlpha()
    alpha.update(100.0, 100.0)  # init: total=200
    sig = alpha.update(200.0, 100.0)  # total=300, delta=+100, bid>ask → +
    assert sig > 0.0


def test_depth_increase_ask_dominant_negative() -> None:
    """Depth grows + ask > bid → negative signal."""
    alpha = DepthReplenishmentAlpha()
    alpha.update(100.0, 100.0)  # init: total=200
    sig = alpha.update(100.0, 200.0)  # total=300, delta=+100, ask>bid → -
    assert sig < 0.0


def test_depth_decrease_bid_dominant_negative() -> None:
    """Depth shrinks + bid > ask → sign(bid-ask)=+1, delta<0 → negative raw → neg signal."""
    alpha = DepthReplenishmentAlpha()
    alpha.update(200.0, 100.0)  # init: total=300
    sig = alpha.update(150.0, 50.0)  # total=200, delta=-100, bid>ask → raw=-100
    assert sig < 0.0


def test_equal_sides_zero_sign() -> None:
    """Equal bid/ask → sign=0 → raw_dr=0 regardless of delta."""
    alpha = DepthReplenishmentAlpha()
    alpha.update(100.0, 100.0)  # init
    sig = alpha.update(200.0, 200.0)  # delta=+200, sign(0)=0 → raw=0
    assert sig == 0.0


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_converges_constant_input() -> None:
    """EMA should converge to the raw DR given constant input."""
    alpha = DepthReplenishmentAlpha()
    alpha.update(100.0, 100.0)  # init
    # Constant: bid=300, ask=100 → total=400. After init total=200, delta=+200 first,
    # then delta=0 every subsequent tick. So converges to 0.
    for _ in range(100):
        alpha.update(300.0, 100.0)
    # delta_depth = 0 after second tick, side_sign = +1 → raw_dr = 0
    # EMA converges to 0
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-4)


def test_ema_second_step_manual() -> None:
    """Verify EMA arithmetic on second step."""
    alpha = DepthReplenishmentAlpha()
    alpha.update(100.0, 100.0)  # init, total=200

    # Step 2: bid=200, ask=100 → total=300, delta=+100, sign(+100)=+1 → raw=+100
    sig2 = alpha.update(200.0, 100.0)
    # EMA starts at 0, first real update: ema = 0 + alpha*(100 - 0)
    expected = _EMA_ALPHA * 100.0
    assert sig2 == pytest.approx(expected, abs=1e-9)


def test_ema_two_steps_manual() -> None:
    """Verify EMA arithmetic across two real steps."""
    alpha = DepthReplenishmentAlpha()
    alpha.update(100.0, 100.0)  # init, total=200

    # Step 2: total=300, delta=+100, bid>ask → raw=+100
    alpha.update(200.0, 100.0)
    ema1 = _EMA_ALPHA * 100.0

    # Step 3: total=250, delta=-50, bid=100<ask=150 → sign=-1 → raw=+50
    sig3 = alpha.update(100.0, 150.0)
    raw3 = (-50.0) * (-1.0)  # delta=-50, sign(100-150)=-1 → raw=+50
    expected = ema1 + _EMA_ALPHA * (raw3 - ema1)
    assert sig3 == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = DepthReplenishmentAlpha()
    alpha.update(bid_qty=100.0, ask_qty=100.0)  # init
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    assert sig > 0.0


def test_update_one_arg_raises() -> None:
    alpha = DepthReplenishmentAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_reset_clears_state() -> None:
    alpha = DepthReplenishmentAlpha()
    alpha.update(100.0, 100.0)
    alpha.update(300.0, 100.0)
    assert alpha.get_signal() != 0.0
    alpha.reset()
    # After reset, first update returns 0 (no prior depth).
    sig = alpha.update(100.0, 100.0)
    assert sig == 0.0


def test_get_signal_before_update_is_zero() -> None:
    alpha = DepthReplenishmentAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = DepthReplenishmentAlpha()
    assert isinstance(alpha, AlphaProtocol)
