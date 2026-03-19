from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from hft_platform.alpha.batch_correlation import (
    _max_pool_correlation,
    batch_compute_correlations,
)


class TestMaxPoolCorrelation:
    def test_self_excluded(self) -> None:
        sig = np.array([1.0, 2.0, 3.0, 4.0])
        pool = {"self": sig, "other": np.array([1.0, 2.0, 3.0, 4.0])}
        corr = _max_pool_correlation(sig, pool, "self")
        assert corr == pytest.approx(1.0, abs=1e-6)

    def test_uncorrelated(self) -> None:
        np.random.seed(42)
        sig = np.random.randn(1000)
        pool = {"a": sig, "b": np.random.randn(1000)}
        corr = _max_pool_correlation(sig, pool, "a")
        assert corr < 0.15  # should be near zero for random signals

    def test_empty_pool(self) -> None:
        sig = np.array([1.0, 2.0, 3.0])
        corr = _max_pool_correlation(sig, {}, "a")
        assert corr == 0.0

    def test_short_signal(self) -> None:
        sig = np.array([1.0])
        pool = {"a": sig, "b": np.array([2.0])}
        corr = _max_pool_correlation(sig, pool, "a")
        assert corr == 0.0

    def test_constant_signal(self) -> None:
        sig = np.array([1.0, 1.0, 1.0, 1.0])
        pool = {"a": sig, "b": np.array([1.0, 2.0, 3.0, 4.0])}
        corr = _max_pool_correlation(sig, pool, "a")
        assert corr == 0.0  # zero std => skip


class TestBatchComputeCorrelations:
    def test_empty_experiments(self, tmp_path: Path) -> None:
        exp_dir = tmp_path / "experiments"
        exp_dir.mkdir()
        (exp_dir / "runs").mkdir()
        results = batch_compute_correlations(
            experiments_dir=str(exp_dir),
            project_root=str(tmp_path),
        )
        assert results == {}

    def test_dry_run_no_patch(self, tmp_path: Path) -> None:
        # Setup experiment directory with two runs
        exp_dir = tmp_path / "experiments"
        runs_dir = exp_dir / "runs"

        for i, alpha_id in enumerate(["alpha_a", "alpha_b"]):
            run_id = f"run_{i}"
            run_dir = runs_dir / run_id
            run_dir.mkdir(parents=True)

            signal = np.random.RandomState(i).randn(100)
            np.save(run_dir / "signals.npy", signal)

            scorecard = {"sharpe_oos": 1.5}
            (run_dir / "scorecard.json").write_text(json.dumps(scorecard))
            (run_dir / "backtest_report.json").write_text("{}")

            meta = {
                "run_id": run_id,
                "alpha_id": alpha_id,
                "config_hash": "abc",
                "timestamp": f"2026-03-0{i + 1}T00:00:00",
                "data_paths": [],
                "metrics": {"sharpe_oos": 1.5},
                "gate_status": {},
                "scorecard_path": str(run_dir / "scorecard.json"),
                "backtest_report_path": str(run_dir / "backtest_report.json"),
                "signals_path": str(run_dir / "signals.npy"),
                "equity_path": None,
            }
            (run_dir / "meta.json").write_text(json.dumps(meta))

        results = batch_compute_correlations(
            experiments_dir=str(exp_dir),
            project_root=str(tmp_path),
            dry_run=True,
        )
        assert len(results) == 2
        assert "alpha_a" in results
        assert "alpha_b" in results

        # Verify scorecards were NOT patched in dry_run
        for run_dir in runs_dir.iterdir():
            sc = json.loads((run_dir / "scorecard.json").read_text())
            assert "correlation_pool_max" not in sc
