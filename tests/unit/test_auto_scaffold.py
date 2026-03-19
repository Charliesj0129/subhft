from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from research.tools.hypothesis_queue import Hypothesis, HypothesisQueue


class TestAutoScaffoldPipeline:
    def _make_queue_with_hypotheses(
        self, tmp_path: Path, count: int = 3
    ) -> HypothesisQueue:
        queue_path = tmp_path / "hypothesis_queue.json"
        queue = HypothesisQueue(queue_path=queue_path)
        for i in range(count):
            queue._hypotheses.append(
                Hypothesis(
                    paper_ref=str(100 + i),
                    arxiv_id=f"2408.{i:05d}",
                    title=f"Test Paper {i}",
                    hypothesis=f"Hypothesis {i}",
                    formula=f"alpha_t = x_{i}",
                    data_fields=("spread_scaled", "mid_price_x2"),
                    suggested_alpha_id=f"test_alpha_{i}",
                    composite_score=1.0 - i * 0.1,
                    status="pending",
                )
            )
        queue._save()
        return queue

    def test_dry_run_no_scaffold(self, tmp_path: Path) -> None:
        from research.tools.auto_scaffold import AutoScaffoldPipeline

        queue = self._make_queue_with_hypotheses(tmp_path, count=3)
        pipeline = AutoScaffoldPipeline(queue=queue)
        results = pipeline.scaffold_top(n=2, dry_run=True)

        assert len(results) == 2
        assert all(r["status"] == "would_scaffold" for r in results)
        # Queue entries should NOT be marked as scaffolded
        assert all(h.status == "pending" for h in queue.all_hypotheses())

    def test_already_exists_marks_scaffolded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from research.tools import auto_scaffold as _as_mod

        queue = self._make_queue_with_hypotheses(tmp_path, count=1)

        # Create existing alpha dir
        alphas_dir = tmp_path / "alphas"
        alphas_dir.mkdir()
        (alphas_dir / "test_alpha_0").mkdir()
        monkeypatch.setattr(_as_mod, "ALPHAS_DIR", alphas_dir)

        pipeline = _as_mod.AutoScaffoldPipeline(queue=queue)
        results = pipeline.scaffold_top(n=1)

        assert len(results) == 1
        assert results[0]["status"] == "already_exists"
        # Should still mark as scaffolded
        reloaded = HypothesisQueue(queue_path=tmp_path / "hypothesis_queue.json")
        assert reloaded.all_hypotheses()[0].status == "scaffolded"

    def test_scaffold_via_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from research.tools import auto_scaffold as _as_mod

        queue = self._make_queue_with_hypotheses(tmp_path, count=1)
        alphas_dir = tmp_path / "alphas"
        alphas_dir.mkdir()
        monkeypatch.setattr(_as_mod, "ALPHAS_DIR", alphas_dir)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            pipeline = _as_mod.AutoScaffoldPipeline(queue=queue)
            results = pipeline.scaffold_top(n=1)

        assert len(results) == 1
        assert results[0]["status"] == "scaffolded"
        mock_run.assert_called_once()

    def test_scaffold_failure_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from research.tools import auto_scaffold as _as_mod

        queue = self._make_queue_with_hypotheses(tmp_path, count=1)
        alphas_dir = tmp_path / "alphas"
        alphas_dir.mkdir()
        monkeypatch.setattr(_as_mod, "ALPHAS_DIR", alphas_dir)

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "scaffold error"

        with patch("subprocess.run", return_value=mock_result):
            pipeline = _as_mod.AutoScaffoldPipeline(queue=queue)
            results = pipeline.scaffold_top(n=1)

        assert len(results) == 1
        assert results[0]["status"] == "failed"

    def test_skip_no_alpha_id(self, tmp_path: Path) -> None:
        from research.tools.auto_scaffold import AutoScaffoldPipeline

        queue_path = tmp_path / "hypothesis_queue.json"
        queue = HypothesisQueue(queue_path=queue_path)
        queue._hypotheses.append(
            Hypothesis(
                paper_ref="100",
                arxiv_id="2408.00000",
                title="T",
                hypothesis="H",
                formula="F",
                data_fields=(),
                suggested_alpha_id="",  # empty
                composite_score=1.0,
                status="pending",
            )
        )
        queue._save()

        pipeline = AutoScaffoldPipeline(queue=queue)
        results = pipeline.scaffold_top(n=1)
        assert len(results) == 1
        assert results[0]["status"] == "skipped"

    def test_respects_n_limit(self, tmp_path: Path) -> None:
        from research.tools.auto_scaffold import AutoScaffoldPipeline

        queue = self._make_queue_with_hypotheses(tmp_path, count=5)
        pipeline = AutoScaffoldPipeline(queue=queue)
        results = pipeline.scaffold_top(n=2, dry_run=True)
        assert len(results) == 2

    def test_timeout_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess as _sp

        from research.tools import auto_scaffold as _as_mod

        queue = self._make_queue_with_hypotheses(tmp_path, count=1)
        alphas_dir = tmp_path / "alphas"
        alphas_dir.mkdir()
        monkeypatch.setattr(_as_mod, "ALPHAS_DIR", alphas_dir)

        with patch("subprocess.run", side_effect=_sp.TimeoutExpired(cmd="test", timeout=30)):
            pipeline = _as_mod.AutoScaffoldPipeline(queue=queue)
            results = pipeline.scaffold_top(n=1)

        assert len(results) == 1
        assert results[0]["status"] == "timeout"
