from pathlib import Path

import numpy as np

from hft_platform.alpha.experiments import ExperimentTracker
from hft_platform.alpha.pool import (
    compute_pool_matrix,
    evaluate_marginal_alpha,
    flag_redundant_pairs,
    marginal_contribution_test,
    optimize_pool_weights,
)


def test_pool_matrix_and_redundancy(tmp_path: Path):
    base = tmp_path / "experiments"
    tracker = ExperimentTracker(base_dir=base)
    tracker.log_run(
        run_id="run-a",
        alpha_id="alpha_a",
        config_hash="a",
        data_paths=["d.npy"],
        metrics={"sharpe_oos": 1.0},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float64),
        equity=np.array([100.0, 101.0, 102.0, 103.0], dtype=np.float64),
    )
    tracker.log_run(
        run_id="run-b",
        alpha_id="alpha_b",
        config_hash="b",
        data_paths=["d.npy"],
        metrics={"sharpe_oos": 1.1},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([1.1, 2.1, 3.1, 4.1], dtype=np.float64),
        equity=np.array([100.0, 101.0, 102.0, 103.0], dtype=np.float64),
    )

    matrix = compute_pool_matrix(base_dir=str(base))
    assert matrix["alpha_ids"] == ["alpha_a", "alpha_b"]
    assert len(matrix["matrix"]) == 2

    redundant = flag_redundant_pairs(matrix, threshold=0.7)
    assert len(redundant) == 1
    assert redundant[0]["alpha_a"] == "alpha_a"
    assert redundant[0]["alpha_b"] == "alpha_b"


def test_pool_matrix_contains_spearman(tmp_path: Path):
    base = tmp_path / "experiments"
    tracker = ExperimentTracker(base_dir=base)
    tracker.log_run(
        run_id="run-a",
        alpha_id="alpha_a",
        config_hash="a",
        data_paths=["d.npy"],
        metrics={},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([1.0, 2.0, 3.0, 10.0], dtype=np.float64),
        equity=np.array([100.0, 101.0, 102.0, 103.0], dtype=np.float64),
    )
    tracker.log_run(
        run_id="run-b",
        alpha_id="alpha_b",
        config_hash="b",
        data_paths=["d.npy"],
        metrics={},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([3.0, 2.0, 1.0, 0.0], dtype=np.float64),
        equity=np.array([100.0, 100.5, 101.0, 100.7], dtype=np.float64),
    )

    matrix = compute_pool_matrix(base_dir=str(base))
    assert "pearson_matrix" in matrix
    assert "spearman_matrix" in matrix
    redundant = flag_redundant_pairs(matrix, threshold=0.5, metric="spearman")
    assert redundant
    assert redundant[0]["metric"] == "spearman"


def test_optimize_pool_weights_and_marginal(tmp_path: Path):
    base = tmp_path / "experiments"
    tracker = ExperimentTracker(base_dir=base)
    tracker.log_run(
        run_id="run-a",
        alpha_id="alpha_a",
        config_hash="a",
        data_paths=["d.npy"],
        metrics={},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([0.1, 0.2, 0.15, 0.3, 0.25], dtype=np.float64),
        equity=np.array([100.0, 100.2, 100.35, 100.8, 101.0], dtype=np.float64),
    )
    tracker.log_run(
        run_id="run-b",
        alpha_id="alpha_b",
        config_hash="b",
        data_paths=["d.npy"],
        metrics={},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([-0.1, -0.2, -0.15, -0.05, -0.1], dtype=np.float64),
        equity=np.array([100.0, 99.8, 99.7, 99.75, 99.6], dtype=np.float64),
    )
    tracker.log_run(
        run_id="run-c",
        alpha_id="alpha_c",
        config_hash="c",
        data_paths=["d.npy"],
        metrics={},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([0.0, 0.3, -0.2, 0.4, -0.1], dtype=np.float64),
        equity=np.array([100.0, 100.1, 99.9, 100.3, 100.0], dtype=np.float64),
    )

    result = optimize_pool_weights(base_dir=str(base), method="mean_variance")
    assert result.alpha_ids
    assert len(result.weights) == 3
    assert np.isclose(sum(abs(v) for v in result.weights.values()), 1.0)

    marginal = evaluate_marginal_alpha(
        alpha_id="alpha_c",
        base_dir=str(base),
        method="ridge",
        min_uplift=-1.0,  # Force pass for deterministic test.
    )
    assert marginal["alpha_id"] == "alpha_c"
    assert "uplift" in marginal
    assert marginal["passed"] is True


def test_marginal_contribution_without_returns_uses_diversification():
    existing = {
        "a": np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float64),
        "b": np.array([1.1, 2.1, 3.1, 4.1], dtype=np.float64),
    }
    new_signal = np.array([-1.0, -2.0, -3.0, -3.5], dtype=np.float64)
    payload = marginal_contribution_test(
        new_signal=new_signal,
        existing_signals=existing,
        method="equal_weight",
        min_uplift=-1.0,
        returns=None,
    )
    assert payload["metric_name"] == "diversification_score"
    assert payload["returns_used"] is False
