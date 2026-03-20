"""Tests for LatencyAuditResult and latency_audit.py functions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROFILES_YAML = {
    "profiles": {
        "shioaji_sim_p95_v2026-03-04": {
            "description": "Test profile",
            "submit_ack_latency_ms": 36.0,
            "modify_ack_latency_ms": 43.0,
            "cancel_ack_latency_ms": 47.0,
            "local_decision_pipeline_latency_us": 250,
            "measurement_date": "2026-03-04",
        }
    }
}

_COMPLIANT_SCORECARD = {
    "alpha_id": "good_alpha",
    "latency_profile": {
        "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
        "submit_ack_latency_ms": 36.0,
        "modify_ack_latency_ms": 43.0,
        "cancel_ack_latency_ms": 47.0,
    },
    "stress_test": {"passed": True},
}

_NO_PROFILE_SCORECARD = {
    "alpha_id": "no_profile_alpha",
    # no latency_profile key at all
    "stress_test": {"passed": True},
}

_INVALID_PROFILE_SCORECARD = {
    "alpha_id": "invalid_profile_alpha",
    "latency_profile": {
        "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
        # submit_ack_latency_ms is below 80% of 36.0 (= 28.8)
        "submit_ack_latency_ms": 10.0,
        "modify_ack_latency_ms": 43.0,
        "cancel_ack_latency_ms": 47.0,
    },
    "stress_test": {"passed": True},
}

_UNKNOWN_PROFILE_SCORECARD = {
    "alpha_id": "unknown_profile_alpha",
    "latency_profile": {
        "latency_profile_id": "nonexistent_profile_id",
        "submit_ack_latency_ms": 36.0,
        "modify_ack_latency_ms": 43.0,
        "cancel_ack_latency_ms": 47.0,
    },
    "stress_test": {"passed": True},
}

_MISSING_STRESS_SCORECARD = {
    "alpha_id": "no_stress_alpha",
    "latency_profile": {
        "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
        "submit_ack_latency_ms": 36.0,
        "modify_ack_latency_ms": 43.0,
        "cancel_ack_latency_ms": 47.0,
    },
    # no stress_test key
}

_FAILED_STRESS_SCORECARD = {
    "alpha_id": "failed_stress_alpha",
    "latency_profile": {
        "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
        "submit_ack_latency_ms": 36.0,
        "modify_ack_latency_ms": 43.0,
        "cancel_ack_latency_ms": 47.0,
    },
    "stress_test": {"passed": False},
}


def _write_profiles(root: Path, data: dict | None = None) -> None:
    """Write latency_profiles.yaml under config/research/."""
    config_dir = root / "config" / "research"
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = data if data is not None else _PROFILES_YAML
    (config_dir / "latency_profiles.yaml").write_text(yaml.dump(payload), encoding="utf-8")


def _write_scorecard(base: Path, alpha_id: str, scorecard: dict, sub: str = "validations") -> Path:
    """Write a scorecard.json under research/experiments/<sub>/<alpha_id>/."""
    alpha_dir = base / "research" / "experiments" / sub / alpha_id
    alpha_dir.mkdir(parents=True, exist_ok=True)
    path = alpha_dir / "scorecard.json"
    path.write_text(json.dumps(scorecard), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Import check
# ---------------------------------------------------------------------------


def test_module_imports() -> None:
    """Module imports without error."""
    from hft_platform.alpha import latency_audit  # noqa: F401

    assert latency_audit is not None


# ---------------------------------------------------------------------------
# LatencyAuditResult dataclass
# ---------------------------------------------------------------------------


class TestLatencyAuditResult:
    def test_frozen(self) -> None:
        from hft_platform.alpha.latency_audit import LatencyAuditResult

        r = LatencyAuditResult(
            alpha_id="a",
            has_profile=True,
            profile_valid=True,
            stress_tested=True,
            issues=(),
        )
        with pytest.raises((AttributeError, TypeError)):
            r.alpha_id = "b"  # type: ignore[misc]

    def test_issues_is_tuple(self) -> None:
        from hft_platform.alpha.latency_audit import LatencyAuditResult

        r = LatencyAuditResult(
            alpha_id="a",
            has_profile=False,
            profile_valid=False,
            stress_tested=False,
            issues=("issue1", "issue2"),
        )
        assert isinstance(r.issues, tuple)
        assert len(r.issues) == 2


# ---------------------------------------------------------------------------
# audit_alphas — empty directory
# ---------------------------------------------------------------------------


class TestAuditAlphasEmpty:
    def test_empty_returns_empty_list(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        # Create the directories but put no scorecard.json files
        (tmp_path / "research" / "experiments" / "validations").mkdir(parents=True)
        (tmp_path / "research" / "experiments" / "promotions").mkdir(parents=True)

        results = audit_alphas(tmp_path)
        assert results == []

    def test_missing_experiments_dir_returns_empty(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        results = audit_alphas(tmp_path)
        assert results == []


# ---------------------------------------------------------------------------
# audit_alphas — missing profiles YAML
# ---------------------------------------------------------------------------


class TestAuditAlpasMissingProfilesYaml:
    def test_missing_yaml_all_has_profile_false(self, tmp_path: Path) -> None:
        """When latency_profiles.yaml is missing, profiles cannot be validated."""
        from hft_platform.alpha.latency_audit import audit_alphas

        # No profiles YAML written
        _write_scorecard(tmp_path, "alpha_a", _COMPLIANT_SCORECARD)
        _write_scorecard(tmp_path, "alpha_b", _NO_PROFILE_SCORECARD)

        results = audit_alphas(tmp_path)
        assert len(results) == 2

        # alpha_a has a latency_profile dict, so has_profile=True;
        # but profile_valid=False because we can't look up the reference.
        alpha_a = next(r for r in results if r.alpha_id == "good_alpha")
        assert alpha_a.has_profile is True
        assert alpha_a.profile_valid is False
        assert any("unavailable" in issue.lower() for issue in alpha_a.issues)

        # alpha_b has no latency_profile at all
        alpha_b = next(r for r in results if r.alpha_id == "no_profile_alpha")
        assert alpha_b.has_profile is False
        assert alpha_b.profile_valid is False

    def test_empty_profiles_yaml(self, tmp_path: Path) -> None:
        """Empty YAML file — graceful handling."""
        from hft_platform.alpha.latency_audit import audit_alphas

        config_dir = tmp_path / "config" / "research"
        config_dir.mkdir(parents=True)
        (config_dir / "latency_profiles.yaml").write_text("", encoding="utf-8")

        _write_scorecard(tmp_path, "alpha_c", _COMPLIANT_SCORECARD)
        results = audit_alphas(tmp_path)
        assert len(results) == 1
        assert results[0].has_profile is True
        assert results[0].profile_valid is False


# ---------------------------------------------------------------------------
# audit_alphas — compliant alpha
# ---------------------------------------------------------------------------


class TestAuditAlphasCompliant:
    def test_compliant_alpha(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        _write_scorecard(tmp_path, "good_alpha", _COMPLIANT_SCORECARD)

        results = audit_alphas(tmp_path)
        assert len(results) == 1
        r = results[0]
        assert r.alpha_id == "good_alpha"
        assert r.has_profile is True
        assert r.profile_valid is True
        assert r.stress_tested is True
        assert r.issues == ()

    def test_compliant_in_promotions_subdir(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        _write_scorecard(tmp_path, "promoted_alpha", _COMPLIANT_SCORECARD, sub="promotions")

        results = audit_alphas(tmp_path)
        assert len(results) == 1
        assert results[0].has_profile is True
        assert results[0].profile_valid is True


# ---------------------------------------------------------------------------
# audit_alphas — non-compliant cases
# ---------------------------------------------------------------------------


class TestAuditAlphasNonCompliant:
    def test_missing_profile(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        _write_scorecard(tmp_path, "no_profile_alpha", _NO_PROFILE_SCORECARD)

        results = audit_alphas(tmp_path)
        assert len(results) == 1
        r = results[0]
        assert r.alpha_id == "no_profile_alpha"
        assert r.has_profile is False
        assert r.profile_valid is False
        assert len(r.issues) >= 1
        assert any("missing" in issue.lower() for issue in r.issues)

    def test_invalid_profile_values(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        _write_scorecard(tmp_path, "invalid_profile_alpha", _INVALID_PROFILE_SCORECARD)

        results = audit_alphas(tmp_path)
        assert len(results) == 1
        r = results[0]
        assert r.has_profile is True
        assert r.profile_valid is False
        assert any("submit_ack_latency_ms" in issue for issue in r.issues)

    def test_unknown_profile_id(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        _write_scorecard(tmp_path, "unknown_profile_alpha", _UNKNOWN_PROFILE_SCORECARD)

        results = audit_alphas(tmp_path)
        assert len(results) == 1
        r = results[0]
        assert r.has_profile is True
        assert r.profile_valid is False
        assert any("not found" in issue for issue in r.issues)

    def test_missing_stress_test(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        _write_scorecard(tmp_path, "no_stress_alpha", _MISSING_STRESS_SCORECARD)

        results = audit_alphas(tmp_path)
        assert len(results) == 1
        r = results[0]
        assert r.stress_tested is False
        # Profile itself is compliant
        assert r.profile_valid is True

    def test_failed_stress_test(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        _write_scorecard(tmp_path, "failed_stress_alpha", _FAILED_STRESS_SCORECARD)

        results = audit_alphas(tmp_path)
        assert len(results) == 1
        r = results[0]
        assert r.stress_tested is False
        assert any("stress_test" in issue for issue in r.issues)


# ---------------------------------------------------------------------------
# audit_alphas — mixed batch
# ---------------------------------------------------------------------------


class TestAuditAlphasMixedBatch:
    def test_mixed_compliant_and_non_compliant(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        _write_scorecard(tmp_path, "good_alpha", _COMPLIANT_SCORECARD)
        _write_scorecard(tmp_path, "bad_alpha", _NO_PROFILE_SCORECARD)
        _write_scorecard(tmp_path, "invalid_alpha", _INVALID_PROFILE_SCORECARD)

        results = audit_alphas(tmp_path)
        assert len(results) == 3

        by_id = {r.alpha_id: r for r in results}
        assert by_id["good_alpha"].profile_valid is True
        assert by_id["no_profile_alpha"].has_profile is False
        assert by_id["invalid_profile_alpha"].profile_valid is False

    def test_both_promotions_and_validations_scanned(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        _write_scorecard(tmp_path, "val_alpha", _COMPLIANT_SCORECARD, sub="validations")
        _write_scorecard(tmp_path, "promo_alpha", {**_COMPLIANT_SCORECARD, "alpha_id": "promo_alpha"}, sub="promotions")

        results = audit_alphas(tmp_path)
        assert len(results) == 2
        ids = {r.alpha_id for r in results}
        assert "good_alpha" in ids
        assert "promo_alpha" in ids

    def test_nested_run_scorecard_discovered(self, tmp_path: Path) -> None:
        """Scorecards nested under run-timestamped subdirs are discovered."""
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        # Nest: validations/my_alpha/20260318T.../scorecard.json
        run_dir = tmp_path / "research" / "experiments" / "validations" / "my_alpha" / "20260318T000000Z_abc"
        run_dir.mkdir(parents=True)
        (run_dir / "scorecard.json").write_text(
            json.dumps({**_COMPLIANT_SCORECARD, "alpha_id": "my_alpha"}), encoding="utf-8"
        )

        results = audit_alphas(tmp_path)
        assert len(results) == 1
        assert results[0].alpha_id == "my_alpha"


# ---------------------------------------------------------------------------
# Gate D tolerance boundary
# ---------------------------------------------------------------------------


class TestGateDTolerance:
    def test_exactly_at_80_percent_threshold_passes(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        # 36.0 * 0.8 = 28.8 — exactly at threshold should pass
        sc = {
            "alpha_id": "threshold_alpha",
            "latency_profile": {
                "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
                "submit_ack_latency_ms": 28.8,
                "modify_ack_latency_ms": 43.0,
                "cancel_ack_latency_ms": 47.0,
            },
            "stress_test": {"passed": True},
        }
        _write_scorecard(tmp_path, "threshold_alpha", sc)

        results = audit_alphas(tmp_path)
        assert results[0].profile_valid is True

    def test_just_below_80_percent_fails(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        # 28.7 < 28.8 should fail
        sc = {
            "alpha_id": "below_threshold_alpha",
            "latency_profile": {
                "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
                "submit_ack_latency_ms": 28.7,
                "modify_ack_latency_ms": 43.0,
                "cancel_ack_latency_ms": 47.0,
            },
            "stress_test": {"passed": True},
        }
        _write_scorecard(tmp_path, "below_threshold_alpha", sc)

        results = audit_alphas(tmp_path)
        assert results[0].profile_valid is False


# ---------------------------------------------------------------------------
# format_audit_report
# ---------------------------------------------------------------------------


class TestFormatAuditReport:
    def test_empty_results(self) -> None:
        from hft_platform.alpha.latency_audit import format_audit_report

        report = format_audit_report([])
        assert "No scorecards found" in report

    def test_report_contains_alpha_id(self) -> None:
        from hft_platform.alpha.latency_audit import LatencyAuditResult, format_audit_report

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

    def test_report_shows_yes_no_flags(self) -> None:
        from hft_platform.alpha.latency_audit import LatencyAuditResult, format_audit_report

        results = [
            LatencyAuditResult(
                alpha_id="compliant",
                has_profile=True,
                profile_valid=True,
                stress_tested=True,
                issues=(),
            ),
            LatencyAuditResult(
                alpha_id="missing",
                has_profile=False,
                profile_valid=False,
                stress_tested=False,
                issues=("no profile",),
            ),
        ]
        report = format_audit_report(results)
        assert "YES" in report
        assert "NO" in report

    def test_report_contains_summary_counts(self) -> None:
        from hft_platform.alpha.latency_audit import LatencyAuditResult, format_audit_report

        results = [
            LatencyAuditResult(
                alpha_id="a",
                has_profile=True,
                profile_valid=True,
                stress_tested=True,
                issues=(),
            ),
            LatencyAuditResult(
                alpha_id="b",
                has_profile=False,
                profile_valid=False,
                stress_tested=False,
                issues=("missing",),
            ),
        ]
        report = format_audit_report(results)
        assert "Total scorecards" in report
        assert "Compliant" in report
        assert "Missing profile" in report

    def test_report_shows_issues(self) -> None:
        from hft_platform.alpha.latency_audit import LatencyAuditResult, format_audit_report

        results = [
            LatencyAuditResult(
                alpha_id="broken",
                has_profile=False,
                profile_valid=False,
                stress_tested=False,
                issues=("latency_profile is missing",),
            )
        ]
        report = format_audit_report(results)
        assert "latency_profile is missing" in report

    def test_report_is_string(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import LatencyAuditResult, format_audit_report

        results = [
            LatencyAuditResult(
                alpha_id="x",
                has_profile=True,
                profile_valid=True,
                stress_tested=True,
                issues=(),
            )
        ]
        report = format_audit_report(results)
        assert isinstance(report, str)
        assert len(report) > 0

    def test_multiple_issues_all_shown(self) -> None:
        from hft_platform.alpha.latency_audit import LatencyAuditResult, format_audit_report

        results = [
            LatencyAuditResult(
                alpha_id="multi_issue",
                has_profile=True,
                profile_valid=False,
                stress_tested=False,
                issues=("issue one", "issue two", "issue three"),
            )
        ]
        report = format_audit_report(results)
        assert "issue one" in report
        assert "issue two" in report
        assert "issue three" in report


# ---------------------------------------------------------------------------
# Malformed scorecard handling
# ---------------------------------------------------------------------------


class TestMalformedScorecard:
    def test_invalid_json_scorecard(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        bad_dir = tmp_path / "research" / "experiments" / "validations" / "bad_json"
        bad_dir.mkdir(parents=True)
        (bad_dir / "scorecard.json").write_text("NOT VALID JSON {{{", encoding="utf-8")

        results = audit_alphas(tmp_path)
        assert len(results) == 1
        r = results[0]
        assert r.has_profile is False
        assert r.profile_valid is False
        assert len(r.issues) >= 1
        assert any("parse error" in issue.lower() or "error" in issue.lower() for issue in r.issues)

    def test_null_latency_profile(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        sc = {"alpha_id": "null_profile", "latency_profile": None}
        _write_scorecard(tmp_path, "null_profile", sc)

        results = audit_alphas(tmp_path)
        assert len(results) == 1
        assert results[0].has_profile is False

    def test_empty_latency_profile_dict(self, tmp_path: Path) -> None:
        from hft_platform.alpha.latency_audit import audit_alphas

        _write_profiles(tmp_path)
        sc = {"alpha_id": "empty_profile", "latency_profile": {}}
        _write_scorecard(tmp_path, "empty_profile", sc)

        results = audit_alphas(tmp_path)
        assert len(results) == 1
        assert results[0].has_profile is False
