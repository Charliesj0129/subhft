from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np

from hft_platform.alpha.paper_trade_batch import (
    batch_record_sessions,
    discover_gate_d_candidates,
)


def _setup_experiment(
    tmp_path: Path,
    alpha_id: str,
    sharpe_oos: float = 1.5,
    max_drawdown: float = 0.1,
    correlation_pool_max: float = 0.3,
) -> Path:
    """Create a minimal experiment run for testing."""
    exp_dir = tmp_path / "experiments"
    runs_dir = exp_dir / "runs"
    run_id = f"run_{alpha_id}"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    scorecard = {
        "sharpe_oos": sharpe_oos,
        "max_drawdown": max_drawdown,
        "correlation_pool_max": correlation_pool_max,
    }
    sc_path = run_dir / "scorecard.json"
    sc_path.write_text(json.dumps(scorecard))
    (run_dir / "backtest_report.json").write_text("{}")

    signal = np.random.RandomState(42).randn(100)
    sig_path = run_dir / "signals.npy"
    np.save(sig_path, signal)

    meta = {
        "run_id": run_id,
        "alpha_id": alpha_id,
        "config_hash": "abc",
        "timestamp": "2026-03-01T00:00:00",
        "data_paths": [],
        "metrics": {"sharpe_oos": sharpe_oos},
        "gate_status": {},
        "scorecard_path": str(sc_path),
        "backtest_report_path": str(run_dir / "backtest_report.json"),
        "signals_path": str(sig_path),
        "equity_path": None,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta))
    return exp_dir


class TestDiscoverGateDCandidates:
    def test_finds_passing_alphas(self, tmp_path: Path) -> None:
        exp_dir = _setup_experiment(tmp_path, "good_alpha", sharpe_oos=2.0)
        candidates = discover_gate_d_candidates(experiments_dir=str(exp_dir))
        assert len(candidates) == 1
        assert candidates[0]["alpha_id"] == "good_alpha"

    def test_filters_low_sharpe(self, tmp_path: Path) -> None:
        exp_dir = _setup_experiment(tmp_path, "bad_alpha", sharpe_oos=0.5)
        candidates = discover_gate_d_candidates(experiments_dir=str(exp_dir))
        assert len(candidates) == 0

    def test_filters_high_drawdown(self, tmp_path: Path) -> None:
        exp_dir = _setup_experiment(tmp_path, "dd_alpha", max_drawdown=0.5)
        candidates = discover_gate_d_candidates(experiments_dir=str(exp_dir))
        assert len(candidates) == 0

    def test_filters_high_correlation(self, tmp_path: Path) -> None:
        exp_dir = _setup_experiment(tmp_path, "corr_alpha", correlation_pool_max=0.9)
        candidates = discover_gate_d_candidates(experiments_dir=str(exp_dir))
        assert len(candidates) == 0

    def test_top_n_limit(self, tmp_path: Path) -> None:
        exp_dir = None
        for i in range(5):
            exp_dir = _setup_experiment(tmp_path, f"alpha_{i}", sharpe_oos=2.0 + i * 0.1)
        assert exp_dir is not None
        candidates = discover_gate_d_candidates(experiments_dir=str(exp_dir), top_n=3)
        assert len(candidates) <= 3


class TestBatchRecordSessions:
    def test_generates_sessions(self, tmp_path: Path) -> None:
        exp_dir = tmp_path / "experiments"
        exp_dir.mkdir()
        (exp_dir / "runs").mkdir()
        (exp_dir / "paper_trade").mkdir()

        results = batch_record_sessions(
            alpha_ids=["test_alpha"],
            experiments_dir=str(exp_dir),
            sessions_per_alpha=3,
            base_date="2026-03-02",  # Monday — avoids weekend edge cases
            seed=42,
        )
        assert len(results) == 3
        assert all(r["alpha_id"] == "test_alpha" for r in results)
        # Verify different trading days
        days = {r["trading_day"] for r in results}
        assert len(days) == 3

    def test_skips_existing_sessions(self, tmp_path: Path) -> None:
        exp_dir = tmp_path / "experiments"
        exp_dir.mkdir()
        (exp_dir / "runs").mkdir()

        from hft_platform.alpha.experiments import ExperimentTracker

        tracker = ExperimentTracker(base_dir=exp_dir)
        # Pre-record 5 sessions
        for i in range(5):
            tracker.log_paper_trade_session(
                alpha_id="test_alpha",
                trading_day=f"2026-03-{10 + i:02d}",
                fills=10,
                pnl_bps=1.0,
            )

        results = batch_record_sessions(
            alpha_ids=["test_alpha"],
            experiments_dir=str(exp_dir),
            sessions_per_alpha=5,
        )
        assert len(results) == 0  # Already has enough

    def test_deterministic_with_seed(self, tmp_path: Path) -> None:
        exp_dir = tmp_path / "experiments"
        exp_dir.mkdir()
        (exp_dir / "runs").mkdir()

        r1 = batch_record_sessions(
            alpha_ids=["alpha_a"],
            experiments_dir=str(exp_dir),
            sessions_per_alpha=3,
            seed=123,
        )
        # Reset for second run
        shutil.rmtree(exp_dir / "paper_trade", ignore_errors=True)

        r2 = batch_record_sessions(
            alpha_ids=["alpha_a"],
            experiments_dir=str(exp_dir),
            sessions_per_alpha=3,
            seed=123,
        )
        assert [r["fills"] for r in r1] == [r["fills"] for r in r2]

    def test_regimes_covered(self, tmp_path: Path) -> None:
        exp_dir = tmp_path / "experiments"
        exp_dir.mkdir()
        (exp_dir / "runs").mkdir()

        results = batch_record_sessions(
            alpha_ids=["test_alpha"],
            experiments_dir=str(exp_dir),
            sessions_per_alpha=8,
            seed=42,
        )
        regimes = {r["regime"] for r in results}
        assert len(regimes) >= 2  # Should cover multiple regimes with 8 sessions


class TestDiscoverGateDCandidatesAdditional:
    def test_skips_missing_scorecard_file(self, tmp_path: Path) -> None:
        """Line 51: scorecard path in meta points to non-existent file — alpha skipped."""
        exp_dir = tmp_path / "experiments"
        runs_dir = exp_dir / "runs"
        run_dir = runs_dir / "run_missing_sc"
        run_dir.mkdir(parents=True)

        sc_path = run_dir / "scorecard_MISSING.json"  # deliberately absent
        (run_dir / "backtest_report.json").write_text("{}")
        signal = np.random.RandomState(0).randn(50)
        sig_path = run_dir / "signals.npy"
        np.save(sig_path, signal)

        meta = {
            "run_id": "run_missing_sc",
            "alpha_id": "missing_sc_alpha",
            "config_hash": "abc",
            "timestamp": "2026-03-01T00:00:00",
            "data_paths": [],
            "metrics": {},
            "gate_status": {},
            "scorecard_path": str(sc_path),
            "backtest_report_path": str(run_dir / "backtest_report.json"),
            "signals_path": str(sig_path),
            "equity_path": None,
        }
        (run_dir / "meta.json").write_text(json.dumps(meta))

        candidates = discover_gate_d_candidates(experiments_dir=str(exp_dir))
        assert candidates == []

    def test_skips_invalid_json_scorecard(self, tmp_path: Path) -> None:
        """Lines 54-55: corrupt scorecard JSON causes alpha to be skipped."""
        exp_dir = tmp_path / "experiments"
        runs_dir = exp_dir / "runs"
        run_dir = runs_dir / "run_bad_json"
        run_dir.mkdir(parents=True)

        sc_path = run_dir / "scorecard.json"
        sc_path.write_text("NOT VALID JSON <<<")
        (run_dir / "backtest_report.json").write_text("{}")
        signal = np.random.RandomState(0).randn(50)
        sig_path = run_dir / "signals.npy"
        np.save(sig_path, signal)

        meta = {
            "run_id": "run_bad_json",
            "alpha_id": "bad_json_alpha",
            "config_hash": "abc",
            "timestamp": "2026-03-01T00:00:00",
            "data_paths": [],
            "metrics": {},
            "gate_status": {},
            "scorecard_path": str(sc_path),
            "backtest_report_path": str(run_dir / "backtest_report.json"),
            "signals_path": str(sig_path),
            "equity_path": None,
        }
        (run_dir / "meta.json").write_text(json.dumps(meta))

        candidates = discover_gate_d_candidates(experiments_dir=str(exp_dir))
        assert candidates == []

    def test_skips_duplicate_alpha_id_runs(self, tmp_path: Path) -> None:
        """Line 44: second run with same alpha_id is skipped — only first kept."""
        exp_dir = tmp_path / "experiments"
        runs_dir = exp_dir / "runs"
        alpha_id = "dup_alpha"

        # Create two runs for the same alpha_id; both pass Gate D
        for i in range(2):
            run_dir = runs_dir / f"run_{i}"
            run_dir.mkdir(parents=True)
            sc_path = run_dir / "scorecard.json"
            sc_path.write_text(
                json.dumps(
                    {
                        "sharpe_oos": 2.0 + i,
                        "max_drawdown": 0.05,
                        "correlation_pool_max": 0.1,
                    }
                )
            )
            (run_dir / "backtest_report.json").write_text("{}")
            signal = np.random.RandomState(i).randn(50)
            sig_path = run_dir / "signals.npy"
            np.save(sig_path, signal)
            meta = {
                "run_id": f"run_{i}",
                "alpha_id": alpha_id,
                "config_hash": "abc",
                "timestamp": f"2026-03-0{i + 1}T00:00:00",
                "data_paths": [],
                "metrics": {"sharpe_oos": 2.0 + i},
                "gate_status": {},
                "scorecard_path": str(sc_path),
                "backtest_report_path": str(run_dir / "backtest_report.json"),
                "signals_path": str(sig_path),
                "equity_path": None,
            }
            (run_dir / "meta.json").write_text(json.dumps(meta))

        candidates = discover_gate_d_candidates(experiments_dir=str(exp_dir))
        # Despite two runs, only one candidate per alpha_id
        assert len(candidates) == 1
        assert candidates[0]["alpha_id"] == alpha_id

    def test_skips_alpha_with_enough_sessions(self, tmp_path: Path) -> None:
        """Line 74: alpha with session_count >= 5 is excluded from candidates."""
        from hft_platform.alpha.experiments import ExperimentTracker

        exp_dir = _setup_experiment(tmp_path, "rich_alpha", sharpe_oos=2.0)

        # Pre-record 5 sessions so session_count >= 5
        tracker = ExperimentTracker(base_dir=exp_dir)
        for i in range(5):
            tracker.log_paper_trade_session(
                alpha_id="rich_alpha",
                trading_day=f"2026-03-{10 + i:02d}",
                fills=20,
                pnl_bps=1.5,
            )

        candidates = discover_gate_d_candidates(experiments_dir=str(exp_dir))
        assert all(c["alpha_id"] != "rich_alpha" for c in candidates)
