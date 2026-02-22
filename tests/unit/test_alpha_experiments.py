from pathlib import Path

import numpy as np

from hft_platform.alpha.experiments import ExperimentTracker


def test_experiment_tracker_log_and_list(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    meta_path = tracker.log_run(
        run_id="run-1",
        alpha_id="ofi_mc",
        config_hash="cfg1",
        data_paths=["/tmp/feed.npy"],
        metrics={"sharpe_oos": 1.2, "max_drawdown": -0.1},
        gate_status={"gate_c": True},
        scorecard_payload={"sharpe_oos": 1.2},
        backtest_report_payload={"gate": "Gate C", "passed": True},
        signals=np.array([0.1, 0.2, 0.3], dtype=np.float64),
        equity=np.array([100.0, 101.0, 102.0], dtype=np.float64),
    )
    assert meta_path.exists()

    rows = tracker.list_runs()
    assert len(rows) == 1
    assert rows[0].run_id == "run-1"
    assert rows[0].alpha_id == "ofi_mc"


def test_experiment_tracker_compare_and_best(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    tracker.log_run(
        run_id="run-a",
        alpha_id="ofi_mc",
        config_hash="a",
        data_paths=["d1.npy"],
        metrics={"sharpe_oos": 0.8},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([0.1, 0.2], dtype=np.float64),
        equity=np.array([100.0, 101.0], dtype=np.float64),
    )
    tracker.log_run(
        run_id="run-b",
        alpha_id="ofi_mc",
        config_hash="b",
        data_paths=["d2.npy"],
        metrics={"sharpe_oos": 1.5},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([0.3, 0.4], dtype=np.float64),
        equity=np.array([100.0, 102.0], dtype=np.float64),
    )

    compared = tracker.compare(["run-b", "run-a"])
    assert [row["run_id"] for row in compared] == ["run-b", "run-a"]

    best = tracker.best_by_metric("sharpe_oos", n=1)
    assert len(best) == 1
    assert best[0]["run_id"] == "run-b"


def test_experiment_tracker_latest_equity_and_proxy_returns(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    tracker.log_run(
        run_id="run-a",
        alpha_id="alpha_a",
        config_hash="a",
        data_paths=["d1.npy"],
        metrics={},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float64),
        equity=np.array([100.0, 101.0, 102.0, 103.0], dtype=np.float64),
    )
    tracker.log_run(
        run_id="run-b",
        alpha_id="alpha_b",
        config_hash="b",
        data_paths=["d2.npy"],
        metrics={},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([0.4, 0.3, 0.2, 0.1], dtype=np.float64),
        equity=np.array([100.0, 99.0, 99.5, 100.0], dtype=np.float64),
    )

    eq = tracker.latest_equity_by_alpha()
    assert sorted(eq) == ["alpha_a", "alpha_b"]
    returns = tracker.proxy_returns()
    assert returns is not None
    assert returns.size == 3
