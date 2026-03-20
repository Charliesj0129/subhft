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


# ---------------------------------------------------------------------------
# discover_gate_e_candidates
# ---------------------------------------------------------------------------


class TestDiscoverGateECandidates:
    def test_returns_only_gate_d_passed_gate_e_not_passed(self, tmp_path: Path) -> None:
        """Only alphas with gate_d_passed=True and gate_e_passed=False are returned."""
        _make_decision(tmp_path, "alpha_ok", gate_d_passed=True, gate_e_passed=False)
        _make_decision(tmp_path, "alpha_both_passed", gate_d_passed=True, gate_e_passed=True)
        _make_decision(tmp_path, "alpha_d_failed", gate_d_passed=False, gate_e_passed=False)
        _make_decision(tmp_path, "alpha_d_failed_e_passed", gate_d_passed=False, gate_e_passed=True)

        candidates = discover_gate_e_candidates(tmp_path)

        assert len(candidates) == 1
        assert candidates[0]["alpha_id"] == "alpha_ok"
        assert candidates[0]["gate_d_passed"] is True
        assert candidates[0]["gate_e_passed"] is False

    def test_candidate_contains_decision_path(self, tmp_path: Path) -> None:
        """Each candidate dict includes a 'decision_path' key pointing to the JSON file."""
        decision_path = _make_decision(tmp_path, "my_alpha", gate_d_passed=True, gate_e_passed=False)

        candidates = discover_gate_e_candidates(tmp_path)

        assert len(candidates) == 1
        assert candidates[0]["decision_path"] == str(decision_path)

    def test_multiple_candidates_returned(self, tmp_path: Path) -> None:
        """Multiple qualifying alphas are all discovered."""
        _make_decision(tmp_path, "alpha_a", gate_d_passed=True, gate_e_passed=False)
        _make_decision(tmp_path, "alpha_b", gate_d_passed=True, gate_e_passed=False)
        _make_decision(tmp_path, "alpha_c", gate_d_passed=True, gate_e_passed=True)

        candidates = discover_gate_e_candidates(tmp_path)

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

        candidates = discover_gate_e_candidates(tmp_path)

        assert len(candidates) == 1
        assert candidates[0]["reasons"] == ["sharpe_below_threshold"]
        assert candidates[0]["canary_weight"] == 0.1

    def test_empty_promotions_dir_returns_empty_list(self, tmp_path: Path) -> None:
        """An empty promotions directory yields an empty candidate list."""
        promotions_dir = tmp_path.joinpath(*_PROMOTIONS_SUBPATH)
        promotions_dir.mkdir(parents=True)

        candidates = discover_gate_e_candidates(tmp_path)

        assert candidates == []

    def test_missing_promotions_dir_returns_empty_list(self, tmp_path: Path) -> None:
        """When the promotions directory does not exist, an empty list is returned."""
        candidates = discover_gate_e_candidates(tmp_path)

        assert candidates == []

    def test_invalid_json_files_skipped_gracefully(self, tmp_path: Path) -> None:
        """Files that contain invalid JSON are skipped without raising."""
        bad_dir = tmp_path.joinpath(*_PROMOTIONS_SUBPATH, "bad_alpha", "20260101T000000Z_00000000")
        bad_dir.mkdir(parents=True)
        (bad_dir / "promotion_decision.json").write_text("NOT JSON {{{{", encoding="utf-8")

        # A valid candidate should still be returned alongside the bad file.
        _make_decision(tmp_path, "good_alpha", gate_d_passed=True, gate_e_passed=False)

        candidates = discover_gate_e_candidates(tmp_path)

        assert len(candidates) == 1
        assert candidates[0]["alpha_id"] == "good_alpha"

    def test_non_dict_json_skipped_gracefully(self, tmp_path: Path) -> None:
        """A JSON file whose root is not a dict is skipped without raising."""
        weird_dir = tmp_path.joinpath(*_PROMOTIONS_SUBPATH, "weird_alpha", "20260101T000000Z_eeeeeeee")
        weird_dir.mkdir(parents=True)
        (weird_dir / "promotion_decision.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        candidates = discover_gate_e_candidates(tmp_path)

        assert candidates == []

    def test_multiple_runs_for_same_alpha_all_qualifying_included(self, tmp_path: Path) -> None:
        """Multiple timestamp subdirs for the same alpha_id are treated independently."""
        _make_decision(
            tmp_path, "repeat_alpha", gate_d_passed=True, gate_e_passed=False, timestamp="20260101T000000Z_aaa"
        )
        _make_decision(
            tmp_path, "repeat_alpha", gate_d_passed=True, gate_e_passed=False, timestamp="20260102T000000Z_bbb"
        )

        candidates = discover_gate_e_candidates(tmp_path)

        assert len(candidates) == 2
        assert all(c["alpha_id"] == "repeat_alpha" for c in candidates)


# ---------------------------------------------------------------------------
# GateEBatchRunner — dry_run
# ---------------------------------------------------------------------------


class TestGateEBatchRunnerDryRun:
    def test_dry_run_lists_candidates_no_execution(self, tmp_path: Path) -> None:
        """Dry-run returns all candidates; completed/failed/skipped are empty."""
        _make_decision(tmp_path, "alpha_x", gate_d_passed=True, gate_e_passed=False)
        _make_decision(tmp_path, "alpha_y", gate_d_passed=True, gate_e_passed=False)
        _make_decision(tmp_path, "alpha_done", gate_d_passed=True, gate_e_passed=True)

        config = GateEBatchConfig(project_root=tmp_path, dry_run=True)
        runner = GateEBatchRunner()
        report = runner.run(config)

        candidate_ids = {c["alpha_id"] for c in report.candidates}
        assert candidate_ids == {"alpha_x", "alpha_y"}
        assert report.completed == ()
        assert report.failed == ()
        assert report.skipped == ()

    def test_dry_run_does_not_invoke_campaign_runner(self, tmp_path: Path) -> None:
        """campaign_runner must NOT be called in dry-run mode."""
        _make_decision(tmp_path, "alpha_z", gate_d_passed=True, gate_e_passed=False)

        called: list[str] = []

        def campaign_runner(alpha_id: str) -> None:
            called.append(alpha_id)

        config = GateEBatchConfig(project_root=tmp_path, dry_run=True)
        runner = GateEBatchRunner(campaign_runner=campaign_runner)
        runner.run(config)

        assert called == []

    def test_dry_run_writes_report_file(self, tmp_path: Path) -> None:
        """Dry-run still writes gate_e_batch_report.json."""
        _make_decision(tmp_path, "alpha_w", gate_d_passed=True, gate_e_passed=False)

        config = GateEBatchConfig(project_root=tmp_path, dry_run=True)
        GateEBatchRunner().run(config)

        report_path = tmp_path / "research" / "experiments" / "gate_e_batch_report.json"
        assert report_path.exists()
        payload = json.loads(report_path.read_text())
        assert isinstance(payload["candidates"], list)
        assert payload["completed"] == []
        assert payload["failed"] == []
        assert payload["skipped"] == []


# ---------------------------------------------------------------------------
# GateEBatchRunner — live run (no campaign_runner)
# ---------------------------------------------------------------------------


class TestGateEBatchRunnerNoCampaignRunner:
    def test_no_runner_skips_all_candidates(self, tmp_path: Path) -> None:
        """When no campaign_runner is provided, all candidates are skipped."""
        _make_decision(tmp_path, "alpha_1", gate_d_passed=True, gate_e_passed=False)
        _make_decision(tmp_path, "alpha_2", gate_d_passed=True, gate_e_passed=False)

        config = GateEBatchConfig(project_root=tmp_path, dry_run=False)
        runner = GateEBatchRunner(campaign_runner=None)
        report = runner.run(config)

        assert set(report.skipped) == {"alpha_1", "alpha_2"}
        assert report.completed == ()
        assert report.failed == ()


# ---------------------------------------------------------------------------
# GateEBatchRunner — live run (with campaign_runner)
# ---------------------------------------------------------------------------


class TestGateEBatchRunnerWithCampaignRunner:
    def test_successful_run_marks_completed(self, tmp_path: Path) -> None:
        """Alphas whose campaign_runner call succeeds appear in 'completed'."""
        _make_decision(tmp_path, "alpha_pass", gate_d_passed=True, gate_e_passed=False)

        def campaign_runner(alpha_id: str) -> None:
            pass  # success

        config = GateEBatchConfig(project_root=tmp_path, dry_run=False)
        report = GateEBatchRunner(campaign_runner=campaign_runner).run(config)

        assert "alpha_pass" in report.completed
        assert report.failed == ()
        assert report.skipped == ()

    def test_failing_runner_marks_failed(self, tmp_path: Path) -> None:
        """Alphas whose campaign_runner raises appear in 'failed'."""
        _make_decision(tmp_path, "alpha_fail", gate_d_passed=True, gate_e_passed=False)

        def campaign_runner(alpha_id: str) -> None:
            raise RuntimeError("paper trade exploded")

        config = GateEBatchConfig(project_root=tmp_path, dry_run=False)
        report = GateEBatchRunner(campaign_runner=campaign_runner).run(config)

        assert "alpha_fail" in report.failed
        assert report.completed == ()

    def test_mixed_results(self, tmp_path: Path) -> None:
        """Some candidates complete, some fail, none skipped when runner is provided."""
        _make_decision(tmp_path, "alpha_ok", gate_d_passed=True, gate_e_passed=False, timestamp="20260101T000000Z_aaa")
        _make_decision(
            tmp_path, "alpha_err", gate_d_passed=True, gate_e_passed=False, timestamp="20260101T000000Z_bbb"
        )

        def campaign_runner(alpha_id: str) -> None:
            if alpha_id == "alpha_err":
                raise ValueError("boom")

        config = GateEBatchConfig(project_root=tmp_path, dry_run=False)
        report = GateEBatchRunner(campaign_runner=campaign_runner).run(config)

        assert "alpha_ok" in report.completed
        assert "alpha_err" in report.failed
        assert report.skipped == ()

    def test_runner_receives_correct_alpha_id(self, tmp_path: Path) -> None:
        """campaign_runner is called with the exact alpha_id from the JSON."""
        _make_decision(tmp_path, "my_special_alpha", gate_d_passed=True, gate_e_passed=False)

        received: list[str] = []

        def campaign_runner(alpha_id: str) -> None:
            received.append(alpha_id)

        config = GateEBatchConfig(project_root=tmp_path, dry_run=False)
        GateEBatchRunner(campaign_runner=campaign_runner).run(config)

        assert received == ["my_special_alpha"]


# ---------------------------------------------------------------------------
# GateEBatchReport structure
# ---------------------------------------------------------------------------


class TestGateEBatchReport:
    def test_report_is_frozen_dataclass(self) -> None:
        """GateEBatchReport is immutable (frozen=True)."""
        report = GateEBatchReport(candidates=(), completed=(), failed=(), skipped=())
        with pytest.raises(AttributeError):
            report.completed = ("oops",)  # type: ignore[misc]

    def test_report_fields_are_tuples(self, tmp_path: Path) -> None:
        """All collection fields on the returned report are tuples."""
        _make_decision(tmp_path, "alpha_t", gate_d_passed=True, gate_e_passed=False)

        config = GateEBatchConfig(project_root=tmp_path, dry_run=True)
        report = GateEBatchRunner().run(config)

        assert isinstance(report.candidates, tuple)
        assert isinstance(report.completed, tuple)
        assert isinstance(report.failed, tuple)
        assert isinstance(report.skipped, tuple)

    def test_report_file_json_structure(self, tmp_path: Path) -> None:
        """The written JSON report has the expected top-level keys."""
        _make_decision(tmp_path, "alpha_j", gate_d_passed=True, gate_e_passed=False)

        config = GateEBatchConfig(project_root=tmp_path, dry_run=False)
        GateEBatchRunner().run(config)

        report_path = tmp_path / "research" / "experiments" / "gate_e_batch_report.json"
        payload = json.loads(report_path.read_text())

        assert set(payload.keys()) >= {"candidates", "completed", "failed", "skipped"}
        assert isinstance(payload["candidates"], list)
        assert isinstance(payload["completed"], list)
        assert isinstance(payload["failed"], list)
        assert isinstance(payload["skipped"], list)

    def test_empty_promotions_produces_empty_report(self, tmp_path: Path) -> None:
        """No candidates → report with all-empty collections."""
        promotions_dir = tmp_path.joinpath(*_PROMOTIONS_SUBPATH)
        promotions_dir.mkdir(parents=True)

        config = GateEBatchConfig(project_root=tmp_path, dry_run=False)
        report = GateEBatchRunner().run(config)

        assert report.candidates == ()
        assert report.completed == ()
        assert report.failed == ()
        assert report.skipped == ()
