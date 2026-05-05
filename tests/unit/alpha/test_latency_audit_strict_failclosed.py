"""Tests for the strict-mode fail-closed semantics of ``latency_audit``.

Slice B Task 12 introduces a runtime ``latency_audit(profile, *,
strict)`` helper used by ``_evaluate_gate_d`` to verify broker P95 fields are
populated on the resolved latency profile before approving promotion.

Under ``strict=False`` (default / loose profile) the audit returns advisory
PASS when ``submit_ack_latency_ms`` / ``cancel_ack_latency_ms`` are absent. Under
``strict=True`` (vm_ul6_strict profile), the same condition fails closed.

The ``v2026-04-24_measured`` Shioaji profile (submit P95=395ms, cancel P95=59ms)
is the canonical live-broker reference; the asymmetry is informational only.
"""

from __future__ import annotations

import pytest

from hft_platform.alpha.latency_audit import latency_audit

# ---------------------------------------------------------------------------
# Test 1: Profile missing submit/cancel P95 + strict=True → fails closed.
# ---------------------------------------------------------------------------


def test_strict_fails_closed_when_profile_p95_missing():
    """Profile dict lacks submit_ack_latency_ms / cancel_ack_latency_ms.

    Under strict mode, this is a hard FAIL — the alpha cannot enter Gate D
    without recording the broker P95 fields.
    """
    profile_missing_p95 = {
        "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
        # Note: submit_ack_latency_ms and cancel_ack_latency_ms intentionally absent.
    }

    result = latency_audit(profile_missing_p95, strict=True)

    assert result["passed"] is False
    assert "submit" in result["reason"] or "cancel" in result["reason"]
    assert "strict" in result["reason"].lower() or "missing" in result["reason"].lower()


# ---------------------------------------------------------------------------
# Test 2: Profile missing submit/cancel P95 + strict=False → advisory PASS.
# ---------------------------------------------------------------------------


def test_loose_returns_advisory_pass_when_profile_p95_missing():
    """Same shape as Test 1, but under loose profile (strict=False).

    Maintains backward-compatible advisory behaviour for non-strict promotion
    runs (e.g. exploratory ``make research`` flows that don't gate on P95).
    """
    profile_missing_p95 = {
        "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
    }

    result = latency_audit(profile_missing_p95, strict=False)

    assert result["passed"] is True
    assert "advisory" in result["reason"].lower()


# ---------------------------------------------------------------------------
# Test 3: Profile present + submit P95 within budget → PASS.
# ---------------------------------------------------------------------------


def test_strict_passes_when_profile_p95_within_budget():
    """Profile populates both submit and cancel P95 fields → PASS under strict."""
    profile = {
        "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
        "submit_ack_latency_ms": 36.0,
        "cancel_ack_latency_ms": 47.0,
    }

    result = latency_audit(profile, strict=True)

    assert result["passed"] is True


# ---------------------------------------------------------------------------
# Test 4: Profile present but P95 above budget → FAIL.
# ---------------------------------------------------------------------------


def test_strict_fails_when_profile_p95_above_budget():
    """Profile populates fields but submit P95 exceeds the configured max budget."""
    profile = {
        "latency_profile_id": "synthetic_overbudget",
        "submit_ack_latency_ms": 5_000.0,
        "cancel_ack_latency_ms": 5_000.0,
    }

    # Tight 100 ms budget — both fields exceed it.
    result = latency_audit(profile, strict=True, max_submit_ms=100.0, max_cancel_ms=100.0)

    assert result["passed"] is False
    assert "budget" in result["reason"].lower() or "exceed" in result["reason"].lower()


# ---------------------------------------------------------------------------
# Test 5: v2026-04-24_measured asymmetric profile — informational only.
# ---------------------------------------------------------------------------


def test_v20260424_measured_profile_passes_under_strict():
    """The canonical Shioaji live-broker profile (asymmetric place vs cancel)
    must pass the strict audit when no explicit budget is configured. The
    asymmetry (submit 395 ms vs cancel 59 ms = 6.7x) is informational — only
    the explicit P95 budget check applies.
    """
    profile = {
        "latency_profile_id": "r47_maker_shioaji_p95_v2026-04-24_measured",
        "submit_ack_latency_ms": 395.0,
        "cancel_ack_latency_ms": 59.0,
    }

    result = latency_audit(profile, strict=True)

    # No explicit budget → fields-present check passes; asymmetry is not auto-fail.
    assert result["passed"] is True
    # The audit must echo the canonical ID so test fixtures can certify provenance.
    assert result.get("profile_id") == "r47_maker_shioaji_p95_v2026-04-24_measured"


# ---------------------------------------------------------------------------
# Edge: keyword-only enforcement for ``strict`` parameter.
# ---------------------------------------------------------------------------


def test_strict_must_be_keyword_only():
    """``strict`` must be keyword-only to prevent accidental positional misuse."""
    profile = {
        "submit_ack_latency_ms": 36.0,
        "cancel_ack_latency_ms": 47.0,
    }
    with pytest.raises(TypeError):
        latency_audit(profile, True)  # type: ignore[misc]
