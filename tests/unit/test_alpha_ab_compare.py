"""Tests for P3c: hft alpha ab-compare CLI command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hft_platform.alpha.experiments import ExperimentTracker


def _seed_run(tracker: ExperimentTracker, run_id: str, metrics: dict) -> None:
    """Helper to seed an experiment run in tracker's base_dir."""
    tracker.log_run(
        run_id=run_id,
        alpha_id="test_alpha",
        config_hash="abc123",
        data_paths=["data/test.npz"],
        metrics=metrics,
        gate_status={"gate_c": True},
        scorecard_payload=dict(metrics),
        backtest_report_payload={},
    )


def test_ab_compare_delta_table(tmp_path: Path, capsys):
    """Two runs with known metrics should produce correct delta values in stdout."""
    tracker = ExperimentTracker(base_dir=str(tmp_path))
    _seed_run(tracker, "run_a", {"sharpe_oos": 1.05, "max_drawdown": -0.18, "turnover": 1.8})
    _seed_run(tracker, "run_b", {"sharpe_oos": 1.32, "max_drawdown": -0.12, "turnover": 2.3})

    from hft_platform.cli import cmd_alpha_ab_compare

    args = argparse.Namespace(run_id_a="run_a", run_id_b="run_b", base_dir=str(tmp_path), out=None)
    cmd_alpha_ab_compare(args)
    captured = capsys.readouterr().out

    # sharpe delta = 1.32 - 1.05 = +0.270
    assert "+0.270" in captured
    assert "sharpe_oos" in captured
    assert "run_a" in captured
    assert "run_b" in captured


def test_ab_compare_missing_run_exits(tmp_path: Path):
    """Non-existent run_id should cause SystemExit with code 1."""
    from hft_platform.cli import cmd_alpha_ab_compare

    tracker = ExperimentTracker(base_dir=str(tmp_path))
    _seed_run(tracker, "run_a", {"sharpe_oos": 1.0})

    args = argparse.Namespace(run_id_a="run_a", run_id_b="run_nonexistent", base_dir=str(tmp_path), out=None)
    with pytest.raises(SystemExit) as exc_info:
        cmd_alpha_ab_compare(args)
    assert exc_info.value.code == 1


def test_ab_compare_output_json(tmp_path: Path, capsys):
    """--out flag should write a JSON file with run_a and run_b keys."""
    tracker = ExperimentTracker(base_dir=str(tmp_path))
    _seed_run(tracker, "run_x", {"sharpe_oos": 1.1, "max_drawdown": -0.15})
    _seed_run(tracker, "run_y", {"sharpe_oos": 1.4, "max_drawdown": -0.10})

    out_path = tmp_path / "comparison.json"
    from hft_platform.cli import cmd_alpha_ab_compare

    args = argparse.Namespace(run_id_a="run_x", run_id_b="run_y", base_dir=str(tmp_path), out=str(out_path))
    cmd_alpha_ab_compare(args)

    assert out_path.exists()
    data = json.loads(out_path.read_text())
    assert "run_a" in data
    assert "run_b" in data
    assert data["run_a"]["run_id"] == "run_x"
    assert data["run_b"]["run_id"] == "run_y"
