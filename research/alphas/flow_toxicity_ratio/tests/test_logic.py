"""Gate B correctness tests for FlowToxicityRatioAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.flow_toxicity_ratio.impl import (
    ALPHA_CLASS,
    FlowToxicityRatioAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert FlowToxicityRatioAlpha().manifest.alpha_id == "flow_toxicity_ratio"


def test_manifest_data_fields() -> None:
    fields = FlowToxicityRatioAlpha().manifest.data_fields
    assert "ofi_l1_raw" in fields
    assert "l1_bid_qty" in fields
    assert "l1_ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    assert FlowToxicityRatioAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert FlowToxicityRatioAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = FlowToxicityRatioAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is FlowToxicityRatioAlpha


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_initial_zero() -> None:
    alpha = FlowToxicityRatioAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_large_ofi_small_queue_high() -> None:
    """Large OFI with small queue → high toxicity."""
    alpha = FlowToxicityRatioAlpha()
    for _ in range(30):
        alpha.update(500.0, 10.0, 10.0)
    # ratio = 500 / 20 = 25.0 → converges to 25.0
    assert alpha.get_signal() > 20.0


def test_small_ofi_large_queue_low() -> None:
    """Small OFI with large queue → low toxicity."""
    alpha = FlowToxicityRatioAlpha()
    for _ in range(30):
        alpha.update(1.0, 5000.0, 5000.0)
    # ratio = 1 / 10000 = 0.0001
    assert alpha.get_signal() < 0.001


def test_zero_ofi_zero() -> None:
    """Zero OFI → signal is zero."""
    alpha = FlowToxicityRatioAlpha()
    sig = alpha.update(0.0, 100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_always_non_negative() -> None:
    """Signal must always be >= 0 (unsigned)."""
    alpha = FlowToxicityRatioAlpha()
    rng = np.random.default_rng(42)
    ofis = rng.uniform(-1000, 1000, 200)
    bids = rng.uniform(0, 1000, 200)
    asks = rng.uniform(0, 1000, 200)
    for o, b, a in zip(ofis, bids, asks):
        sig = alpha.update(o, b, a)
        assert sig >= 0.0, f"Signal was negative: {sig}"


def test_symmetric_ofi_sign() -> None:
    """Positive and negative OFI of same magnitude give same result."""
    a1 = FlowToxicityRatioAlpha()
    a2 = FlowToxicityRatioAlpha()
    sig1 = a1.update(100.0, 50.0, 50.0)
    sig2 = a2.update(-100.0, 50.0, 50.0)
    assert sig1 == pytest.approx(sig2, abs=1e-9)


def test_zero_queue_handled() -> None:
    """Zero queue depth uses max(..., 1) guard → no ZeroDivisionError."""
    alpha = FlowToxicityRatioAlpha()
    sig = alpha.update(50.0, 0.0, 0.0)
    # denom = max(0, 1) = 1 → ratio = 50
    assert sig == pytest.approx(50.0, abs=1e-9)


def test_bounded() -> None:
    """With reasonable inputs, signal stays in a finite range."""
    alpha = FlowToxicityRatioAlpha()
    rng = np.random.default_rng(99)
    for _ in range(500):
        ofi = rng.uniform(-100, 100)
        bid = rng.uniform(50, 200)
        ask = rng.uniform(50, 200)
        sig = alpha.update(ofi, bid, ask)
        # max ratio = 100 / max(100, 1) = 1.0
        assert sig <= 2.0, f"Signal unexpectedly large: {sig}"


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_convergence() -> None:
    """EMA should converge to the raw ratio given constant input."""
    alpha = FlowToxicityRatioAlpha()
    ofi, bid, ask = 80.0, 100.0, 100.0
    expected_ratio = abs(ofi) / max(bid + ask, 1.0)
    for _ in range(200):
        alpha.update(ofi, bid, ask)
    assert alpha.get_signal() == pytest.approx(expected_ratio, abs=1e-4)


def test_stable_ratio() -> None:
    """Constant ratio input produces stable signal after warmup."""
    alpha = FlowToxicityRatioAlpha()
    for _ in range(100):
        alpha.update(20.0, 100.0, 100.0)
    sig1 = alpha.get_signal()
    for _ in range(50):
        alpha.update(20.0, 100.0, 100.0)
    sig2 = alpha.get_signal()
    assert sig1 == pytest.approx(sig2, abs=1e-6)


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_reset() -> None:
    alpha = FlowToxicityRatioAlpha()
    alpha.update(500.0, 10.0, 10.0)
    alpha.reset()
    assert alpha.get_signal() == 0.0
    # After reset, first update equals raw ratio
    sig = alpha.update(0.0, 100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_get_signal() -> None:
    alpha = FlowToxicityRatioAlpha()
    sig = alpha.update(60.0, 100.0, 100.0)
    assert alpha.get_signal() == sig


def test_kwargs() -> None:
    alpha = FlowToxicityRatioAlpha()
    sig = alpha.update(ofi_l1_raw=60.0, l1_bid_qty=100.0, l1_ask_qty=100.0)
    expected = abs(60.0) / max(100.0 + 100.0, 1.0)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_positional() -> None:
    alpha = FlowToxicityRatioAlpha()
    sig = alpha.update(60.0, 100.0, 100.0)
    expected = abs(60.0) / max(100.0 + 100.0, 1.0)
    assert sig == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = FlowToxicityRatioAlpha()
    assert isinstance(alpha, AlphaProtocol)
