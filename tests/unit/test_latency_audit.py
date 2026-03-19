"""Tests for LatencyAuditResult and latency_audit.py functions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hft_platform.alpha.latency_audit import (
    LatencyAuditResult,
    audit_alphas,
    format_audit_report,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_scorecard(project_root: Path, alpha_id: str, run_id: str, payload: dict[str, Any]) -> Path:
    """Write a scorecard.json under research/experiments/validations/<alpha_id>/<run_id>/."""
    sc_dir = project_root / "research" / "experiments" / "validations" / alpha_id / run_id
    sc_dir.mkdir(parents=True, exist_ok=True)
    sc_path = sc_dir / "scorecard.json"
    sc_path.write_text(json.dumps(payload))
    return sc_path


def _make_profiles_yaml(project_root: Path) -> None:
    cfg_dir = project_root / "config" / "research"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "latency_profiles.yaml").write_text(
        """
profiles:
  test_profile_v1:
    submit_ack_latency_ms: 40.0
    modify_ack_latency_ms: 50.0
    cancel_ack_latency_ms: 45.0
"""
    )


# ---------------------------------------------------------------------------
# Tests for audit_alphas
# ---------------------------------------------------------------------------


class TestAuditAlphas:
    def test_no_scorecards_returns_empty(self, tmp_path: Path) -> None:
        results = audit_alphas(tmp_path)
        assert results == []

    def test_scorecard_without_latency_profile(self, tmp_path: Path) -> None:
        _make_scorecard(tmp_path, "alpha_a", "run1", {"sharpe_oos": 1.5})
        results = audit_alphas(tmp_path)
        assert len(results) == 1
        r = results[0]
        assert r.has_profile is False
        assert r.profile_valid is False
        assert any("Missing latency_profile" in i for i in r.issues)

    def test_scorecard_with_valid_latency_profile(self, tmp_path: Path) -> None:
        _make_profiles_yaml(tmp_path)
        _make_scorecard(
            tmp_path,
            "alpha_b",
            "run1",
            {
                "sharpe_oos": 1.5,
                "latency_profile": {
                    "latency_profile_id": "test_profile_v1",
                    "submit_ack_latency_ms": 40.0,
                    "modify_ack_latency_ms": 50.0,
                    "cancel_ack_latency_ms": 45.0,
                },
                "stress_test": {"passed": True},
            },
        )
        results = audit_alphas(tmp_path)
        assert len(results) == 1
        r = results[0]
        assert r.has_profile is True
        assert r.profile_valid is True
        assert r.stress_tested is True
        assert r.issues == ()

    def test_scorecard_with_unrealistic_latency_values(self, tmp_path: Path) -> None:
        _make_profiles_yaml(tmp_path)
        _make_scorecard(
            tmp_path,
            "alpha_c",
            "run1",
            {
                "sharpe_oos": 1.5,
                "latency_profile": {
                    "latency_profile_id": "test_profile_v1",
                    "submit_ack_latency_ms": 0.5,  # too low
                    "modify_ack_latency_ms": 50.0,
                    "cancel_ack_latency_ms": 45.0,
                },
            },
        )
        results = audit_alphas(tmp_path)
        assert len(results) == 1
        r = results[0]
        assert r.profile_valid is False
        assert len(r.issues) >= 1

    def test_promotions_dir_also_scanned(self, tmp_path: Path) -> None:
        promo_dir = tmp_path / "research" / "experiments" / "promotions" / "alpha_d" / "run1"
        promo_dir.mkdir(parents=True, exist_ok=True)
        (promo_dir / "scorecard.json").write_text(json.dumps({"sharpe_oos": 1.5}))
        results = audit_alphas(tmp_path)
        assert any(r.alpha_id == "alpha_d" for r in results)

    def test_invalid_json_returns_error_result(self, tmp_path: Path) -> None:
        sc_dir = tmp_path / "research" / "experiments" / "validations" / "alpha_bad" / "run1"
        sc_dir.mkdir(parents=True, exist_ok=True)
        (sc_dir / "scorecard.json").write_text("not valid json {{")
        results = audit_alphas(tmp_path)
        assert len(results) == 1
        r = results[0]
        assert r.has_profile is False
        assert len(r.issues) == 1

    def test_string_latency_profile_passes(self, tmp_path: Path) -> None:
        _make_scorecard(
            tmp_path,
            "alpha_e",
            "run1",
            {
                "sharpe_oos": 1.5,
                "latency_profile": "shioaji_sim_p95_v2026-03-04",
            },
        )
        results = audit_alphas(tmp_path)
        r = results[0]
        assert r.has_profile is True
        assert r.profile_valid is True

    def test_stress_tested_not_present_adds_issue(self, tmp_path: Path) -> None:
        _make_scorecard(
            tmp_path,
            "alpha_f",
            "run1",
            {
                "sharpe_oos": 1.5,
                "latency_profile": "shioaji_sim_p95_v2026-03-04",
                # no stress_test
            },
        )
        results = audit_alphas(tmp_path)
        r = results[0]
        assert any("stress_test" in i for i in r.issues)


# ---------------------------------------------------------------------------
# Tests for format_audit_report
# ---------------------------------------------------------------------------


class TestFormatAuditReport:
    def test_empty_results(self) -> None:
        report = format_audit_report([])
        assert "No scorecards" in report

    def test_ok_result_format(self) -> None:
        results = [
            LatencyAuditResult(
                alpha_id="my_alpha",
                has_profile=True,
                profile_valid=True,
                stress_tested=True,
                issues=(),
            )
        ]
        report = format_audit_report(results)
        assert "my_alpha" in report
        assert "OK" in report
        assert "1 OK" in report

    def test_warn_result_format(self) -> None:
        results = [
            LatencyAuditResult(
                alpha_id="bad_alpha",
                has_profile=False,
                profile_valid=False,
                stress_tested=False,
                issues=("Missing latency_profile",),
            )
        ]
        report = format_audit_report(results)
        assert "bad_alpha" in report
        assert "WARN" in report
        assert "1 with issues" in report

    def test_summary_counts(self) -> None:
        results = [
            LatencyAuditResult("a1", True, True, True, ()),
            LatencyAuditResult("a2", False, False, False, ("issue1",)),
        ]
        report = format_audit_report(results)
        assert "1 OK" in report
        assert "1 with issues" in report
        assert "total 2" in report
