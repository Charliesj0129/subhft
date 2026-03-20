"""Unit tests for canary release orchestrator (Unit 8)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


class TestReleaseOrchestrator:
    def setup_method(self):
        import os
        import sys

        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
        from auto_release_orchestrator import (
            _evaluate_promotion_readiness,
            _is_eligible_commit,
            run_orchestrator,
        )

        self._is_eligible_commit = _is_eligible_commit
        self._evaluate_promotion_readiness = _evaluate_promotion_readiness
        self._run_orchestrator = run_orchestrator

    def test_feat_commit_eligible(self):
        assert self._is_eligible_commit("feat: add new feature") is True

    def test_fix_commit_eligible(self):
        assert self._is_eligible_commit("fix: correct bug") is True

    def test_feat_scoped_eligible(self):
        assert self._is_eligible_commit("feat(alpha): add screener") is True

    def test_chore_not_eligible(self):
        assert self._is_eligible_commit("chore: update deps") is False

    def test_docs_not_eligible(self):
        assert self._is_eligible_commit("docs: update readme") is False

    def test_empty_not_eligible(self):
        assert self._is_eligible_commit("") is False

    def test_readiness_checks(self, tmp_path: Path):
        """Readiness checks pass when required dirs exist."""
        (tmp_path / "scripts" / "release_channel_guard.py").parent.mkdir(parents=True)
        (tmp_path / "scripts" / "release_channel_guard.py").write_text("# guard")
        (tmp_path / "config" / "strategy_promotions").mkdir(parents=True)
        (tmp_path / "tests").mkdir(parents=True)

        result = self._evaluate_promotion_readiness(tmp_path)
        assert result["ready"] is True

    def test_readiness_fails_missing_dirs(self, tmp_path: Path):
        """Readiness fails when required dirs are missing."""
        result = self._evaluate_promotion_readiness(tmp_path)
        assert result["ready"] is False

    @patch("auto_release_orchestrator._get_latest_commit")
    def test_orchestrator_non_eligible(self, mock_commit, tmp_path: Path):
        mock_commit.return_value = {"sha": "abc12345", "message": "chore: update deps"}
        result = self._run_orchestrator(tmp_path)
        assert result["eligible"] is False

    @patch("auto_release_orchestrator._get_latest_commit")
    def test_orchestrator_dry_run(self, mock_commit, tmp_path: Path):
        mock_commit.return_value = {"sha": "abc12345", "message": "feat: add feature"}
        (tmp_path / "scripts" / "release_channel_guard.py").parent.mkdir(parents=True)
        (tmp_path / "scripts" / "release_channel_guard.py").write_text("# guard")
        (tmp_path / "config" / "strategy_promotions").mkdir(parents=True)
        (tmp_path / "tests").mkdir(parents=True)

        result = self._run_orchestrator(tmp_path, dry_run=True)
        assert result["eligible"] is True
        assert result["ready"] is True
        assert result["config_path"] is None
