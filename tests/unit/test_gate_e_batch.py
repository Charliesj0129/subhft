"""Tests for GateEBatchRunner and discovery functions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hft_platform.alpha.gate_e_batch import (
    GateEBatchConfig,
    GateEBatchRunner,
    discover_gate_e_candidates,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(
    runs_dir: Path,
    run_id: str,
    alpha_id: str,
    gate_d_passed: bool = True,
) -> Path:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "run_id": run_id,
        "alpha_id": alpha_id,
        "gate_status": {"gate_d": gate_d_passed},
    }
    (run_dir / "meta.json").write_text(json.dumps(meta))
    return run_dir


def _cfg(**overrides: Any) -> GateEBatchConfig:
    defaults: dict[str, Any] = {
        "owner": "tester",
        "min_shadow_sessions": 1,
        "max_execution_reject_rate": 0.01,
    }
    defaults.update(overrides)
    return GateEBatchConfig(**defaults)


# ---------------------------------------------------------------------------
# discover_gate_e_candidates
# ---------------------------------------------------------------------------


class TestDiscoverGateECandidates:
    def test_no_runs_dir_returns_empty(self, tmp_path: Path) -> None:
        result = discover_gate_e_candidates(tmp_path)
        assert result == []

    def test_discovers_gate_d_passed_runs(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "research" / "experiments" / "runs"
        _make_run(runs_dir, "run1", "alpha_a", gate_d_passed=True)
        _make_run(runs_dir, "run2", "alpha_b", gate_d_passed=False)
        candidates = discover_gate_e_candidates(tmp_path)
        alpha_ids = [a for a, _ in candidates]
        assert "alpha_a" in alpha_ids
        assert "alpha_b" not in alpha_ids

    def test_skips_malformed_meta(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "research" / "experiments" / "runs"
        bad_dir = runs_dir / "bad_run"
        bad_dir.mkdir(parents=True)
        (bad_dir / "meta.json").write_text("not json {{")
        candidates = discover_gate_e_candidates(tmp_path)
        assert candidates == []

    def test_skips_dirs_without_meta(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "research" / "experiments" / "runs"
        empty_dir = runs_dir / "empty_run"
        empty_dir.mkdir(parents=True)
        candidates = discover_gate_e_candidates(tmp_path)
        assert candidates == []


# ---------------------------------------------------------------------------
# GateEBatchRunner
# ---------------------------------------------------------------------------


class TestGateEBatchRunnerDryRun:
    def test_dry_run_skips_all(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "research" / "experiments" / "runs"
        _make_run(runs_dir, "run1", "alpha_a")
        _make_run(runs_dir, "run2", "alpha_b")
        cfg = _cfg(project_root=str(tmp_path), dry_run=True)
        runner = GateEBatchRunner(cfg)
        report = runner.run()
        assert report.total_candidates == 2
        assert report.skipped == 2
        assert report.passed == 0
        assert report.failed == 0
        for r in report.results:
            assert r["dry_run"] is True
            assert r["skipped"] is True

    def test_dry_run_no_candidates(self, tmp_path: Path) -> None:
        cfg = _cfg(project_root=str(tmp_path), dry_run=True)
        runner = GateEBatchRunner(cfg)
        report = runner.run()
        assert report.total_candidates == 0

    def test_report_to_dict(self, tmp_path: Path) -> None:
        cfg = _cfg(project_root=str(tmp_path), dry_run=True)
        runner = GateEBatchRunner(cfg)
        report = runner.run()
        d = report.to_dict()
        assert "total_candidates" in d
        assert "passed" in d
        assert "failed" in d
        assert "skipped" in d
        assert "results" in d


class TestGateEBatchRunnerLive:
    def test_runs_evaluation_for_candidates(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "research" / "experiments" / "runs"
        _make_run(runs_dir, "run1", "alpha_a")
        cfg = _cfg(project_root=str(tmp_path), dry_run=False, min_shadow_sessions=0)
        runner = GateEBatchRunner(cfg)
        report = runner.run()
        assert report.total_candidates == 1
        # Gate E evaluation runs; result is pass or fail (no crash)
        assert report.passed + report.failed + report.skipped == 1

    def test_evaluation_error_counts_as_skipped(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        runs_dir = tmp_path / "research" / "experiments" / "runs"
        _make_run(runs_dir, "run1", "alpha_a")
        cfg = _cfg(project_root=str(tmp_path), dry_run=False)
        runner = GateEBatchRunner(cfg)

        with patch(
            "hft_platform.alpha.gate_e_batch._evaluate_gate_e",
            side_effect=RuntimeError("boom"),
        ):
            report = runner.run()
        assert report.skipped == 1
        assert report.results[0]["skipped"] is True
        assert "boom" in report.results[0]["error"]
