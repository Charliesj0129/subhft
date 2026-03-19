"""Unit tests for pool correlation auto-computation fix (Unit 3)."""

from __future__ import annotations

import json
from pathlib import Path

from hft_platform.alpha.pool import load_pool_signals_from_experiments


class TestLoadPoolSignalsFromExperiments:
    def test_empty_dir(self, tmp_path: Path):
        """Returns empty dict when no runs exist."""
        experiments = tmp_path / "experiments"
        experiments.mkdir()
        (experiments / "runs").mkdir()

        result = load_pool_signals_from_experiments(experiments)
        assert result == {}

    def test_no_runs_dir(self, tmp_path: Path):
        """Returns empty dict when runs dir doesn't exist."""
        experiments = tmp_path / "experiments"
        experiments.mkdir()

        result = load_pool_signals_from_experiments(experiments)
        assert result == {}

    def test_loads_signals_from_scorecards(self, tmp_path: Path):
        """Loads signals from scorecard.json files."""
        runs = tmp_path / "experiments" / "runs"

        # Create run with scorecard + meta
        run_dir = runs / "20260101T000000Z_abc123"
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text(json.dumps({"alpha_id": "alpha_a"}))
        (run_dir / "scorecard.json").write_text(
            json.dumps(
                {
                    "signals": [0.1, 0.2, 0.3],
                    "sharpe_oos": 1.0,
                }
            )
        )

        result = load_pool_signals_from_experiments(tmp_path / "experiments")
        assert "alpha_a" in result
        assert result["alpha_a"] == [0.1, 0.2, 0.3]

    def test_excludes_target_alpha(self, tmp_path: Path):
        """Excludes the target alpha from pool."""
        runs = tmp_path / "experiments" / "runs"

        run_dir = runs / "20260101T000000Z_abc123"
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text(json.dumps({"alpha_id": "alpha_a"}))
        (run_dir / "scorecard.json").write_text(json.dumps({"signals": [0.1]}))

        result = load_pool_signals_from_experiments(
            tmp_path / "experiments",
            exclude_alpha_id="alpha_a",
        )
        assert "alpha_a" not in result

    def test_latest_run_wins(self, tmp_path: Path):
        """When multiple runs exist for same alpha, latest wins."""
        runs = tmp_path / "experiments" / "runs"

        # Older run
        run1 = runs / "20260101T000000Z_old"
        run1.mkdir(parents=True)
        (run1 / "meta.json").write_text(json.dumps({"alpha_id": "alpha_a"}))
        (run1 / "scorecard.json").write_text(json.dumps({"signals": [0.1]}))

        # Newer run
        run2 = runs / "20260102T000000Z_new"
        run2.mkdir(parents=True)
        (run2 / "meta.json").write_text(json.dumps({"alpha_id": "alpha_a"}))
        (run2 / "scorecard.json").write_text(json.dumps({"signals": [0.9]}))

        result = load_pool_signals_from_experiments(tmp_path / "experiments")
        assert result["alpha_a"] == [0.9]

    def test_skips_runs_without_signals(self, tmp_path: Path):
        """Skips scorecards without signals field."""
        runs = tmp_path / "experiments" / "runs"

        run_dir = runs / "20260101T000000Z_abc"
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text(json.dumps({"alpha_id": "alpha_a"}))
        (run_dir / "scorecard.json").write_text(json.dumps({"sharpe_oos": 1.0}))

        result = load_pool_signals_from_experiments(tmp_path / "experiments")
        assert "alpha_a" not in result

    def test_default_correlation_zero_when_empty(self):
        """When pool is empty (first alpha), correlation_pool_max should be 0.0."""
        from research.registry.scorecard import compute_scorecard

        sc = compute_scorecard(
            {
                "signals": [0.1, 0.2],
                "sharpe_is": 1.0,
                "sharpe_oos": 1.0,
                "ic_mean": 0.01,
                "ic_std": 0.005,
                "turnover": 0.5,
                "max_drawdown": -0.1,
                "regime_metrics": {},
                "capacity_estimate": 1e6,
                "latency_profile": {},
            },
            pool_signals={},
        )
        assert sc.correlation_pool_max == 0.0
