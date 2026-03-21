"""Unit tests for alpha.gate_e_batch — Gate E Candidate Discovery & Batch Orchestrator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hft_platform.alpha.gate_e_batch import (
    GateEBatchConfig,
    GateEBatchReport,
    GateEBatchRunner,
    discover_gate_e_candidates,
    discover_gate_e_promotion_candidates,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROMOTIONS_SUBPATH = ("research", "experiments", "promotions")


def _make_decision(
    tmp_path: Path,
    alpha_id: str,
    gate_d_passed: bool,
    gate_e_passed: bool,
    timestamp: str = "20260101T000000Z_aabbccdd",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write a promotion_decision.json under the expected directory structure."""
    decision_dir = tmp_path.joinpath(*_PROMOTIONS_SUBPATH, alpha_id, timestamp)
    decision_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "alpha_id": alpha_id,
        "gate_d_passed": gate_d_passed,
        "gate_e_passed": gate_e_passed,
        "approved": gate_d_passed and gate_e_passed,
        "reasons": [],
    }
    if extra:
        payload.update(extra)
    decision_path = decision_dir / "promotion_decision.json"
    decision_path.write_text(json.dumps(payload), encoding="utf-8")
    return decision_path


def _make_run_meta(
    tmp_path: Path,
    run_name: str,
    alpha_id: str,
    gate_d: bool,
) -> Path:
    """Write a meta.json under research/experiments/runs/<run_name>/."""
    run_dir = tmp_path / "research" / "experiments" / "runs" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "alpha_id": alpha_id,
        "gate_status": {"gate_d": gate_d},
    }
    meta_path = run_dir / "meta.json"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return meta_path


# ---------------------------------------------------------------------------
# discover_gate_e_promotion_candidates (promotions-based)
# ---------------------------------------------------------------------------


class TestDiscoverGateEPromotionCandidates:
    def test_returns_only_gate_d_passed_gate_e_not_passed(self, tmp_path: Path) -> None:
        """Only alphas with gate_d_passed=True and gate_e_passed=False are returned."""
        _make_decision(tmp_path, "alpha_ok", gate_d_passed=True, gate_e_passed=False)
        _make_decision(tmp_path, "alpha_both_passed", gate_d_passed=True, gate_e_passed=True)
        _make_decision(tmp_path, "alpha_d_failed", gate_d_passed=False, gate_e_passed=False)
        _make_decision(tmp_path, "alpha_d_failed_e_passed", gate_d_passed=False, gate_e_passed=True)

        candidates = discover_gate_e_promotion_candidates(tmp_path)

        assert len(candidates) == 1
        assert candidates[0]["alpha_id"] == "alpha_ok"
        assert candidates[0]["gate_d_passed"] is True
        assert candidates[0]["gate_e_passed"] is False

    def test_candidate_contains_decision_path(self, tmp_path: Path) -> None:
        """Each candidate dict includes a 'decision_path' key pointing to the JSON file."""
        decision_path = _make_decision(tmp_path, "my_alpha", gate_d_passed=True, gate_e_passed=False)

        candidates = discover_gate_e_promotion_candidates(tmp_path)

        assert len(candidates) == 1
        assert candidates[0]["decision_path"] == str(decision_path)

    def test_multiple_candidates_returned(self, tmp_path: Path) -> None:
        """Multiple qualifying alphas are all discovered."""
        _make_decision(tmp_path, "alpha_a", gate_d_passed=True, gate_e_passed=False)
        _make_decision(tmp_path, "alpha_b", gate_d_passed=True, gate_e_passed=False)
        _make_decision(tmp_path, "alpha_c", gate_d_passed=True, gate_e_passed=True)

        candidates = discover_gate_e_promotion_candidates(tmp_path)

        alpha_ids = {c["alpha_id"] for c in candidates}
        assert alpha_ids == {"alpha_a", "alpha_b"}

    def test_extra_fields_preserved(self, tmp_path: Path) -> None:
        """Extra fields from the JSON (e.g. 'reasons') are forwarded in the result dict."""
        _make_decision(
            tmp_path,
            "alpha_extra",
            gate_d_passed=True,
            gate_e_passed=False,
            extra={"reasons": ["sharpe_below_threshold"], "canary_weight": 0.1},
        )

        candidates = discover_gate_e_promotion_candidates(tmp_path)

        assert len(candidates) == 1
        assert candidates[0]["reasons"] == ["sharpe_below_threshold"]
        assert candidates[0]["canary_weight"] == 0.1

    def test_empty_promotions_dir_returns_empty_list(self, tmp_path: Path) -> None:
        """An empty promotions directory yields an empty candidate list."""
        promotions_dir = tmp_path.joinpath(*_PROMOTIONS_SUBPATH)
        promotions_dir.mkdir(parents=True)

        candidates = discover_gate_e_promotion_candidates(tmp_path)

        assert candidates == []

    def test_missing_promotions_dir_returns_empty_list(self, tmp_path: Path) -> None:
        """When the promotions directory does not exist, an empty list is returned."""
        candidates = discover_gate_e_promotion_candidates(tmp_path)

        assert candidates == []

    def test_invalid_json_files_skipped_gracefully(self, tmp_path: Path) -> None:
        """Files that contain invalid JSON are skipped without raising."""
        bad_dir = tmp_path.joinpath(*_PROMOTIONS_SUBPATH, "bad_alpha", "20260101T000000Z_00000000")
        bad_dir.mkdir(parents=True)
        (bad_dir / "promotion_decision.json").write_text("NOT JSON {{{{", encoding="utf-8")

        # A valid candidate should still be returned alongside the bad file.
        _make_decision(tmp_path, "good_alpha", gate_d_passed=True, gate_e_passed=False)

        candidates = discover_gate_e_promotion_candidates(tmp_path)

        assert len(candidates) == 1
        assert candidates[0]["alpha_id"] == "good_alpha"

    def test_non_dict_json_skipped_gracefully(self, tmp_path: Path) -> None:
        """A JSON file whose root is not a dict is skipped without raising."""
        weird_dir = tmp_path.joinpath(*_PROMOTIONS_SUBPATH, "weird_alpha", "20260101T000000Z_eeeeeeee")
        weird_dir.mkdir(parents=True)
        (weird_dir / "promotion_decision.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        candidates = discover_gate_e_promotion_candidates(tmp_path)

        assert candidates == []

    def test_multiple_runs_for_same_alpha_all_qualifying_included(self, tmp_path: Path) -> None:
        """Multiple timestamp subdirs for the same alpha_id are treated independently."""
        _make_decision(
            tmp_path, "repeat_alpha", gate_d_passed=True, gate_e_passed=False, timestamp="20260101T000000Z_aaa"
        )
        _make_decision(
            tmp_path, "repeat_alpha", gate_d_passed=True, gate_e_passed=False, timestamp="20260102T000000Z_bbb"
        )

        candidates = discover_gate_e_promotion_candidates(tmp_path)

        assert len(candidates) == 2
        assert all(c["alpha_id"] == "repeat_alpha" for c in candidates)


# ---------------------------------------------------------------------------
# discover_gate_e_candidates (runs-based)
# ---------------------------------------------------------------------------


class TestDiscoverGateECandidates:
    def test_returns_gate_d_passed_runs(self, tmp_path: Path) -> None:
        """Only runs with gate_d=True in meta.json are returned."""
        _make_run_meta(tmp_path, "run_ok", "alpha_ok", gate_d=True)
        _make_run_meta(tmp_path, "run_fail", "alpha_fail", gate_d=False)

        candidates = discover_gate_e_candidates(tmp_path)

        assert len(candidates) == 1
        alpha_id, run_dir = candidates[0]
        assert alpha_id == "alpha_ok"

    def test_missing_runs_dir_returns_empty(self, tmp_path: Path) -> None:
        candidates = discover_gate_e_candidates(tmp_path)
        assert candidates == []

    def test_empty_runs_dir_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "research" / "experiments" / "runs").mkdir(parents=True)
        candidates = discover_gate_e_candidates(tmp_path)
        assert candidates == []

    def test_multiple_candidates(self, tmp_path: Path) -> None:
        _make_run_meta(tmp_path, "run_a", "alpha_a", gate_d=True)
        _make_run_meta(tmp_path, "run_b", "alpha_b", gate_d=True)
        _make_run_meta(tmp_path, "run_c", "alpha_c", gate_d=False)

        candidates = discover_gate_e_candidates(tmp_path)
        alpha_ids = {c[0] for c in candidates}
        assert alpha_ids == {"alpha_a", "alpha_b"}


# ---------------------------------------------------------------------------
# GateEBatchRunner — dry_run
# ---------------------------------------------------------------------------


class TestGateEBatchRunnerDryRun:
    def test_dry_run_skips_all_candidates(self, tmp_path: Path) -> None:
        """Dry-run marks all candidates as skipped."""
        _make_run_meta(tmp_path, "run_x", "alpha_x", gate_d=True)
        _make_run_meta(tmp_path, "run_y", "alpha_y", gate_d=True)

        config = GateEBatchConfig(project_root=tmp_path, dry_run=True)
        runner = GateEBatchRunner(config)
        report = runner.run()

        assert report.total_candidates == 2
        assert report.skipped == 2
        assert report.passed == 0
        assert report.failed == 0

    def test_dry_run_returns_report(self, tmp_path: Path) -> None:
        """Dry-run returns a GateEBatchReport instance."""
        _make_run_meta(tmp_path, "run_w", "alpha_w", gate_d=True)

        config = GateEBatchConfig(project_root=tmp_path, dry_run=True)
        runner = GateEBatchRunner(config)
        report = runner.run()

        assert isinstance(report, GateEBatchReport)


# ---------------------------------------------------------------------------
# GateEBatchReport structure
# ---------------------------------------------------------------------------


class TestGateEBatchReport:
    def test_report_is_frozen_dataclass(self) -> None:
        """GateEBatchReport is immutable (frozen=True)."""
        report = GateEBatchReport(total_candidates=0, passed=0, failed=0, skipped=0, results=())
        with pytest.raises(AttributeError):
            report.passed = 99  # type: ignore[misc]

    def test_report_to_dict(self) -> None:
        """to_dict returns expected structure."""
        report = GateEBatchReport(total_candidates=2, passed=1, failed=0, skipped=1, results=())
        d = report.to_dict()
        assert d["total_candidates"] == 2
        assert d["passed"] == 1
        assert d["failed"] == 0
        assert d["skipped"] == 1
        assert isinstance(d["results"], list)

    def test_empty_report(self, tmp_path: Path) -> None:
        """No candidates produces an empty report."""
        (tmp_path / "research" / "experiments" / "runs").mkdir(parents=True)

        config = GateEBatchConfig(project_root=tmp_path, dry_run=False)
        runner = GateEBatchRunner(config)
        report = runner.run()

        assert report.total_candidates == 0
        assert report.passed == 0
        assert report.failed == 0
        assert report.skipped == 0
        assert report.results == ()
