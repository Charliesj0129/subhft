"""Coverage tests for hft_platform.alpha.pool — missing line ranges.

Targets: AlphaPool init/get/set/len, PoolOptimizationResult.to_dict,
compute_correlation_payload edge cases, flag_redundant_pairs,
optimize_pool_weights methods, marginal_contribution_test, weight
normalization, pool metric, relative uplift, recompute_pool_correlations,
and load_pool_signals_from_experiments.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from hft_platform.alpha.experiments import ExperimentTracker
from hft_platform.alpha.pool import (
    AlphaPool,
    PoolOptimizationResult,
    _coerce_returns,
    _ic_weighted,
    _mean_variance,
    _normalize_weights,
    _pool_metric,
    _relative_uplift,
    _safe_corr,
    compute_correlation_payload,
    flag_redundant_pairs,
    load_pool_signals_from_experiments,
    marginal_contribution_test,
    optimize_pool_weights,
    recompute_pool_correlations,
)

# ---------------------------------------------------------------------------
# AlphaPool (lines 27-30, 32, 36-37, 45-47, 51-52, 55-56)
# ---------------------------------------------------------------------------


def test_alpha_pool_init_with_ids():
    pool = AlphaPool(alpha_ids=["a", "b"])
    assert len(pool) == 2
    weights = pool.get_weights()
    assert abs(weights["a"] - 0.5) < 1e-9
    assert abs(weights["b"] - 0.5) < 1e-9


def test_alpha_pool_init_empty():
    pool = AlphaPool()
    assert len(pool) == 0
    assert pool.get_weights() == {}


def test_alpha_pool_set_weights():
    pool = AlphaPool(alpha_ids=["a"])
    pool.set_weights({"a": 0.3, "b": 0.7})
    assert pool.alpha_ids() == ["a", "b"]
    assert abs(pool.get_weights()["b"] - 0.7) < 1e-9


# ---------------------------------------------------------------------------
# PoolOptimizationResult.to_dict (line 68)
# ---------------------------------------------------------------------------


def test_pool_optimization_result_to_dict():
    result = PoolOptimizationResult(
        method="equal_weight",
        alpha_ids=("a", "b"),
        weights={"a": 0.5, "b": 0.5},
        returns_used=False,
        diagnostics={"strategy": "equal_weight"},
    )
    d = result.to_dict()
    assert d["method"] == "equal_weight"
    assert d["alpha_ids"] == ["a", "b"]
    assert d["returns_used"] is False


# ---------------------------------------------------------------------------
# compute_correlation_payload: single sample (lines 103-105)
# ---------------------------------------------------------------------------


def test_compute_correlation_payload_single_sample():
    signals = {"a": np.array([1.0]), "b": np.array([2.0])}
    result = compute_correlation_payload(signals=signals)
    assert result["sample_length"] == 1
    assert len(result["matrix"]) == 2


def test_compute_correlation_payload_empty():
    result = compute_correlation_payload(signals={})
    assert result["alpha_ids"] == []
    assert result["sample_length"] == 0


# ---------------------------------------------------------------------------
# flag_redundant_pairs: non-square matrix, spearman metric (line 137)
# ---------------------------------------------------------------------------


def test_flag_redundant_pairs_non_square_matrix():
    payload = {"alpha_ids": ["a"], "pearson_matrix": [[1.0, 0.5]]}
    result = flag_redundant_pairs(payload)
    assert result == []


def test_flag_redundant_pairs_spearman():
    payload = {
        "alpha_ids": ["a", "b"],
        "spearman_matrix": [[1.0, 0.9], [0.9, 1.0]],
    }
    result = flag_redundant_pairs(payload, metric="spearman", threshold=0.7)
    assert len(result) == 1
    assert result[0]["metric"] == "spearman"


# ---------------------------------------------------------------------------
# optimize_pool_weights: different methods (lines 167, 177, 216, 229, 243)
# ---------------------------------------------------------------------------


def test_optimize_pool_weights_empty_signals(tmp_path: Path):
    result = optimize_pool_weights(base_dir=str(tmp_path / "empty"), signals={})
    assert result.weights == {}
    assert result.diagnostics["reason"] == "no_signals"


def test_optimize_pool_weights_zero_length_signals(tmp_path: Path):
    result = optimize_pool_weights(
        base_dir=str(tmp_path / "empty"),
        signals={"a": np.array([], dtype=np.float64)},
    )
    assert result.weights == {}
    assert result.diagnostics["reason"] == "invalid_signals"


def test_optimize_pool_weights_equal_weight(tmp_path: Path):
    signals = {
        "a": np.array([1.0, 2.0, 3.0, 4.0]),
        "b": np.array([4.0, 3.0, 2.0, 1.0]),
    }
    result = optimize_pool_weights(
        base_dir=str(tmp_path),
        signals=signals,
        method="equal_weight",
    )
    assert "a" in result.weights
    assert "b" in result.weights
    assert abs(result.weights["a"] - 0.5) < 1e-6


def test_optimize_pool_weights_ic_weighted(tmp_path: Path):
    signals = {
        "a": np.array([1.0, 2.0, 3.0, 4.0]),
        "b": np.array([4.0, 3.0, 2.0, 1.0]),
    }
    returns = np.array([0.01, 0.02, 0.03, 0.04])
    result = optimize_pool_weights(
        base_dir=str(tmp_path),
        signals=signals,
        method="ic_weighted",
        returns=returns,
    )
    assert result.returns_used is True
    assert len(result.weights) == 2


def test_optimize_pool_weights_mean_variance(tmp_path: Path):
    signals = {
        "a": np.array([1.0, 2.0, 3.0, 4.0, 5.0]),
        "b": np.array([5.0, 4.0, 3.0, 2.0, 1.0]),
    }
    result = optimize_pool_weights(
        base_dir=str(tmp_path),
        signals=signals,
        method="mean_variance",
    )
    assert len(result.weights) == 2


def test_optimize_pool_weights_ridge(tmp_path: Path):
    signals = {
        "a": np.array([1.0, 2.0, 3.0, 4.0, 5.0]),
        "b": np.array([5.0, 4.0, 3.0, 2.0, 1.0]),
    }
    returns = np.array([0.01, 0.02, -0.01, 0.03, 0.02])
    result = optimize_pool_weights(
        base_dir=str(tmp_path),
        signals=signals,
        method="ridge",
        returns=returns,
    )
    assert result.returns_used is True
    assert len(result.weights) == 2


# ---------------------------------------------------------------------------
# marginal_contribution_test (lines 289, 318, 321, 337, 350)
# ---------------------------------------------------------------------------


def test_marginal_contribution_test_no_existing():
    result = marginal_contribution_test(
        new_signal=np.array([1.0, 2.0, 3.0]),
        existing_signals={},
    )
    assert result["passed"] is True
    assert result["reason"] == "no_existing_pool"


def test_marginal_contribution_test_insufficient_samples():
    result = marginal_contribution_test(
        new_signal=np.array([1.0]),
        existing_signals={"a": np.array([2.0])},
    )
    assert result["passed"] is False
    assert result["reason"] == "insufficient_samples"


def test_marginal_contribution_test_with_data():
    existing = {
        "a": np.array([1.0, 2.0, 3.0, 4.0]),
        "b": np.array([4.0, 3.0, 2.0, 1.0]),
    }
    new = np.array([1.5, 2.5, 3.5, 4.5])
    result = marginal_contribution_test(
        new_signal=new,
        existing_signals=existing,
        min_uplift=0.001,
    )
    assert "uplift" in result
    assert isinstance(result["passed"], bool)


# ---------------------------------------------------------------------------
# _normalize_weights edge cases (lines 429, 431, 434)
# ---------------------------------------------------------------------------


def test_normalize_weights_all_zeros():
    w = np.array([0.0, 0.0, 0.0])
    result = _normalize_weights(w)
    assert np.allclose(result, 1.0 / 3.0)


def test_normalize_weights_empty():
    w = np.array([], dtype=np.float64)
    result = _normalize_weights(w)
    assert result.size == 0


# ---------------------------------------------------------------------------
# _pool_metric edge cases (lines 453, 464)
# ---------------------------------------------------------------------------


def test_pool_metric_no_returns_single_alpha():
    data = np.array([[1.0, 2.0, 3.0]])
    weights = np.array([1.0])
    metric, name = _pool_metric(data, weights, None)
    assert name == "diversification_score"
    assert abs(metric) < 1e-9  # single alpha => 0


def test_pool_metric_with_returns_short():
    data = np.array([[1.0, 2.0], [2.0, 1.0]])
    weights = np.array([0.5, 0.5])
    returns = np.array([0.01, 0.02])
    metric, name = _pool_metric(data, weights, returns)
    assert name == "pool_sharpe"
    assert abs(metric) < 1e-9  # m < 3


# ---------------------------------------------------------------------------
# _relative_uplift (lines 375-379, 381-387)
# ---------------------------------------------------------------------------


def test_relative_uplift_zero_baseline_positive_candidate():
    result = _relative_uplift(1.0, 0.0)
    assert result == 1.0


def test_relative_uplift_zero_baseline_zero_candidate():
    result = _relative_uplift(0.0, 0.0)
    assert result == 0.0


def test_relative_uplift_normal():
    result = _relative_uplift(1.5, 1.0)
    assert abs(result - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# _coerce_returns (lines 337, 350)
# ---------------------------------------------------------------------------


def test_coerce_returns_none():
    assert _coerce_returns(None) is None


def test_coerce_returns_single_value():
    assert _coerce_returns([1.0]) is None


def test_coerce_returns_valid():
    result = _coerce_returns([1.0, 2.0, 3.0])
    assert result is not None
    assert result.shape == (3,)


# ---------------------------------------------------------------------------
# _ic_weighted fallback cases (lines 375-379)
# ---------------------------------------------------------------------------


def test_ic_weighted_no_returns():
    data = np.array([[1.0, 2.0, 3.0]])
    result = _ic_weighted(data, None)
    assert np.allclose(result, 1.0)


def test_ic_weighted_short_returns():
    data = np.array([[1.0], [2.0]])
    returns = np.array([0.5])
    result = _ic_weighted(data, returns)
    assert result.shape[0] == 2


def test_ic_weighted_all_zero_ic():
    data = np.array([[1.0, 1.0, 1.0], [1.0, 1.0, 1.0]])
    returns = np.array([0.01, 0.02, 0.03])
    result = _ic_weighted(data, returns)
    assert np.allclose(result, 0.5)


# ---------------------------------------------------------------------------
# _mean_variance with returns (line 396, 400, 408)
# ---------------------------------------------------------------------------


def test_mean_variance_with_short_returns(recwarn):
    data = np.array([[1.0], [2.0]])
    returns = np.array([0.5])
    result = _mean_variance(data, returns)
    assert result.shape[0] == 2
    assert not [w for w in recwarn if issubclass(w.category, RuntimeWarning)]


def test_mean_variance_all_zero_mu():
    data = np.array([[1.0, 1.0, 1.0], [1.0, 1.0, 1.0]])
    returns = np.array([1.0, 1.0, 1.0])
    result = _mean_variance(data, returns)
    assert result.shape[0] == 2


# ---------------------------------------------------------------------------
# _safe_corr short input
# ---------------------------------------------------------------------------


def test_safe_corr_short_input():
    result = _safe_corr(np.array([1.0]), np.array([2.0]))
    assert result == 0.0


# ---------------------------------------------------------------------------
# recompute_pool_correlations (lines 509, 513-516, 519-520)
# ---------------------------------------------------------------------------


def test_recompute_pool_correlations_empty(tmp_path: Path):
    result = recompute_pool_correlations(base_dir=str(tmp_path / "empty"))
    assert result["alpha_ids"] == []


def test_recompute_pool_correlations_apply(tmp_path: Path):
    base = tmp_path / "experiments"
    tracker = ExperimentTracker(base_dir=base)
    tracker.log_run(
        run_id="r1",
        alpha_id="a",
        config_hash="h",
        data_paths=[],
        metrics={},
        gate_status={},
        scorecard_payload={"some": "data"},
        backtest_report_payload={},
        signals=np.array([1.0, 2.0, 3.0]),
    )
    tracker.log_run(
        run_id="r2",
        alpha_id="b",
        config_hash="h2",
        data_paths=[],
        metrics={},
        gate_status={},
        scorecard_payload={"some": "data"},
        backtest_report_payload={},
        signals=np.array([3.0, 2.0, 1.0]),
    )
    result = recompute_pool_correlations(base_dir=str(base), apply=True)
    assert "a" in result["correlation_pool_max"]
    assert "b" in result["correlation_pool_max"]
    assert len(result["updated_scorecards"]) > 0


# ---------------------------------------------------------------------------
# load_pool_signals_from_experiments (lines 529, 531, 535, 543-545, 547-549)
# ---------------------------------------------------------------------------


def test_load_pool_signals_no_dir(tmp_path: Path):
    result = load_pool_signals_from_experiments(tmp_path / "nonexistent")
    assert result == {}


def test_load_pool_signals_with_exclusion(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    run_dir = runs_dir / "r1"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"alpha_id": "a1"}))
    (run_dir / "scorecard.json").write_text(json.dumps({"signals": [1.0, 2.0]}))

    run_dir2 = runs_dir / "r2"
    run_dir2.mkdir()
    (run_dir2 / "meta.json").write_text(json.dumps({"alpha_id": "a2"}))
    (run_dir2 / "scorecard.json").write_text(json.dumps({"signals": [3.0, 4.0]}))

    result = load_pool_signals_from_experiments(tmp_path, exclude_alpha_id="a1")
    assert "a1" not in result
    assert "a2" in result


def test_load_pool_signals_missing_scorecard_signals(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    run_dir = runs_dir / "r1"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"alpha_id": "a1"}))
    (run_dir / "scorecard.json").write_text(json.dumps({"no_signals_key": True}))

    result = load_pool_signals_from_experiments(tmp_path)
    assert result == {}


def test_load_pool_signals_no_alpha_id_in_meta(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    run_dir = runs_dir / "r1"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"other": "data"}))
    (run_dir / "scorecard.json").write_text(json.dumps({"signals": [1.0]}))

    result = load_pool_signals_from_experiments(tmp_path)
    assert result == {}
