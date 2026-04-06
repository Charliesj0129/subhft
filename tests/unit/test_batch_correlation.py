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

    def test_patches_scorecard_when_not_dry_run(self, tmp_path: Path) -> None:
        """Lines 80-100: non-dry-run path patches scorecard files."""
        exp_dir = tmp_path / "experiments"
        runs_dir = exp_dir / "runs"

        for i, alpha_id in enumerate(["alpha_a", "alpha_b"]):
            run_id = f"run_{i}"
            run_dir = runs_dir / run_id
            run_dir.mkdir(parents=True)

            signal = np.random.RandomState(i).randn(100)
            np.save(run_dir / "signals.npy", signal)

            scorecard = {"sharpe_oos": 1.5}
            sc_path = run_dir / "scorecard.json"
            sc_path.write_text(json.dumps(scorecard))
            (run_dir / "backtest_report.json").write_text("{}")

            meta = {
                "run_id": run_id,
                "alpha_id": alpha_id,
                "config_hash": "abc",
                "timestamp": f"2026-03-0{i + 1}T00:00:00",
                "data_paths": [],
                "metrics": {"sharpe_oos": 1.5},
                "gate_status": {},
                "scorecard_path": str(sc_path),
                "backtest_report_path": str(run_dir / "backtest_report.json"),
                "signals_path": str(run_dir / "signals.npy"),
                "equity_path": None,
            }
            (run_dir / "meta.json").write_text(json.dumps(meta))

        results = batch_compute_correlations(
            experiments_dir=str(exp_dir),
            project_root=str(tmp_path),
            dry_run=False,
        )
        assert len(results) == 2

        # Verify scorecards were patched
        for run_dir in runs_dir.iterdir():
            sc = json.loads((run_dir / "scorecard.json").read_text())
            assert "correlation_pool_max" in sc
            assert 0.0 <= sc["correlation_pool_max"] <= 1.0

    def test_scorecard_not_found_logs_warning(self, tmp_path: Path) -> None:
        """Lines 84-89: missing scorecard triggers warning path (no crash)."""
        exp_dir = tmp_path / "experiments"
        runs_dir = exp_dir / "runs"

        for i, alpha_id in enumerate(["alpha_a", "alpha_b"]):
            run_id = f"run_{i}"
            run_dir = runs_dir / run_id
            run_dir.mkdir(parents=True)

            signal = np.random.RandomState(i).randn(100)
            np.save(run_dir / "signals.npy", signal)

            # Deliberately point scorecard_path at a non-existent file
            sc_path = run_dir / "scorecard_MISSING.json"
            (run_dir / "backtest_report.json").write_text("{}")

            meta = {
                "run_id": run_id,
                "alpha_id": alpha_id,
                "config_hash": "abc",
                "timestamp": f"2026-03-0{i + 1}T00:00:00",
                "data_paths": [],
                "metrics": {},
                "gate_status": {},
                "scorecard_path": str(sc_path),
                "backtest_report_path": str(run_dir / "backtest_report.json"),
                "signals_path": str(run_dir / "signals.npy"),
                "equity_path": None,
            }
            (run_dir / "meta.json").write_text(json.dumps(meta))

        results = batch_compute_correlations(
            experiments_dir=str(exp_dir),
            project_root=str(tmp_path),
            dry_run=False,
        )
        # Results are computed even when scorecards can't be patched
        assert len(results) == 2

    def test_scorecard_invalid_json_no_crash(self, tmp_path: Path) -> None:
        """Lines 101-106: corrupt JSON in scorecard triggers warning, no exception."""
        exp_dir = tmp_path / "experiments"
        runs_dir = exp_dir / "runs"

        for i, alpha_id in enumerate(["alpha_a", "alpha_b"]):
            run_id = f"run_{i}"
            run_dir = runs_dir / run_id
            run_dir.mkdir(parents=True)

            signal = np.random.RandomState(i).randn(100)
            np.save(run_dir / "signals.npy", signal)

            sc_path = run_dir / "scorecard.json"
            sc_path.write_text("NOT VALID JSON {{{")
            (run_dir / "backtest_report.json").write_text("{}")

            meta = {
                "run_id": run_id,
                "alpha_id": alpha_id,
                "config_hash": "abc",
                "timestamp": f"2026-03-0{i + 1}T00:00:00",
                "data_paths": [],
                "metrics": {},
                "gate_status": {},
                "scorecard_path": str(sc_path),
                "backtest_report_path": str(run_dir / "backtest_report.json"),
                "signals_path": str(run_dir / "signals.npy"),
                "equity_path": None,
            }
            (run_dir / "meta.json").write_text(json.dumps(meta))

        results = batch_compute_correlations(
            experiments_dir=str(exp_dir),
            project_root=str(tmp_path),
            dry_run=False,
        )
        assert len(results) == 2  # correlations still computed despite JSON error

    def test_duplicate_runs_keeps_first_per_alpha(self, tmp_path: Path) -> None:
        """Lines 64->63: duplicate alpha_id in runs — only first run is kept."""
        exp_dir = tmp_path / "experiments"
        runs_dir = exp_dir / "runs"
        alpha_id = "dup_alpha"

        for i in range(2):
            run_id = f"run_{i}"
            run_dir = runs_dir / run_id
            run_dir.mkdir(parents=True)

            signal = np.random.RandomState(i).randn(100)
            np.save(run_dir / "signals.npy", signal)

            sc_path = run_dir / "scorecard.json"
            sc_path.write_text(json.dumps({"sharpe_oos": 1.5 + i}))
            (run_dir / "backtest_report.json").write_text("{}")

            meta = {
                "run_id": run_id,
                "alpha_id": alpha_id,
                "config_hash": "abc",
                "timestamp": f"2026-03-0{i + 1}T00:00:00",
                "data_paths": [],
                "metrics": {},
                "gate_status": {},
                "scorecard_path": str(sc_path),
                "backtest_report_path": str(run_dir / "backtest_report.json"),
                "signals_path": str(run_dir / "signals.npy"),
                "equity_path": None,
            }
            (run_dir / "meta.json").write_text(json.dumps(meta))

        results = batch_compute_correlations(
            experiments_dir=str(exp_dir),
            project_root=str(tmp_path),
            dry_run=False,
        )
        # Both runs share the same alpha_id; result has exactly one entry
        assert len(results) == 1
        assert alpha_id in results

    def test_other_signal_too_short_skipped(self) -> None:
        """Line 31: other signal with < 2 elements is skipped (min_len < 2 branch)."""
        sig = np.array([1.0, 2.0, 3.0, 4.0])
        # "other" has only 1 element — min_len < 2 → continue
        pool = {"self": sig, "short": np.array([5.0])}
        corr = _max_pool_correlation(sig, pool, "self")
        assert corr == 0.0

    def test_lower_corr_not_replacing_max(self) -> None:
        """Line 37->25 false branch: when corr <= max_corr, max_corr is NOT updated."""
        sig = np.array([1.0, 2.0, 3.0, 4.0])
        # "perfect" is perfectly correlated (will set max_corr=1.0)
        # "anti" is also perfectly anti-correlated (abs=1.0); same max
        # "weak" has lower correlation — tests the else branch of `if corr > max_corr`
        weak = np.array([1.0, 1.1, 0.9, 1.0])  # low correlation
        pool = {
            "self": sig,
            "perfect": np.array([2.0, 4.0, 6.0, 8.0]),  # corr=1.0 first
            "weak": weak,
        }
        corr = _max_pool_correlation(sig, pool, "self")
        # max should be 1.0 from "perfect"; "weak" does not replace it
        assert corr == pytest.approx(1.0, abs=1e-6)
