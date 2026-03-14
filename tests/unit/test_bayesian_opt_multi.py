"""Tests for WU8: MultiObjectiveResult in research/tools/bayesian_opt.py"""

from __future__ import annotations

import pytest

from research.tools.bayesian_opt import BayesianOptConfig, MultiObjectiveResult

# ---------------------------------------------------------------------------
# MultiObjectiveResult dataclass
# ---------------------------------------------------------------------------


def _make_pareto_front() -> list[dict]:
    return [
        {
            "trial_number": 0,
            "params": {"signal_threshold": 0.1},
            "sharpe_oos": 1.5,
            "abs_drawdown": 0.2,
            "turnover_dist": 0.0,
        },
        {
            "trial_number": 1,
            "params": {"signal_threshold": 0.3},
            "sharpe_oos": 2.0,
            "abs_drawdown": 0.35,
            "turnover_dist": 0.05,
        },
    ]


def test_multi_objective_result_fields() -> None:
    """MultiObjectiveResult should be a frozen dataclass with required fields."""
    front = _make_pareto_front()
    result = MultiObjectiveResult(
        pareto_front=front,
        n_trials=50,
        neighbor_consistency=0.75,
    )

    assert result.pareto_front is front
    assert result.n_trials == 50
    assert result.neighbor_consistency == 0.75


def test_multi_objective_result_is_frozen() -> None:
    """MultiObjectiveResult should be immutable (frozen=True)."""
    result = MultiObjectiveResult(
        pareto_front=[],
        n_trials=10,
        neighbor_consistency=0.0,
    )
    with pytest.raises(Exception):  # FrozenInstanceError (dataclasses.FrozenInstanceError)
        result.n_trials = 99  # type: ignore[misc]


def test_multi_objective_result_to_dict_roundtrip() -> None:
    """to_dict should serialize all fields to a plain dict."""
    front = _make_pareto_front()
    result = MultiObjectiveResult(
        pareto_front=front,
        n_trials=30,
        neighbor_consistency=0.5,
    )

    d = result.to_dict()

    assert isinstance(d, dict)
    assert d["n_trials"] == 30
    assert isinstance(d["neighbor_consistency"], float)
    assert d["neighbor_consistency"] == 0.5
    assert isinstance(d["pareto_front"], list)
    assert len(d["pareto_front"]) == len(front)

    # Pareto front entries should match original structure.
    for original, serialized in zip(front, d["pareto_front"]):
        assert serialized["trial_number"] == original["trial_number"]
        assert serialized["sharpe_oos"] == original["sharpe_oos"]
        assert serialized["abs_drawdown"] == original["abs_drawdown"]


def test_multi_objective_result_to_dict_neighbor_consistency_float() -> None:
    """neighbor_consistency must be serialized as float, not int."""
    result = MultiObjectiveResult(pareto_front=[], n_trials=5, neighbor_consistency=1)
    d = result.to_dict()
    assert isinstance(d["neighbor_consistency"], float)


def test_multi_objective_result_empty_pareto_front() -> None:
    """An empty Pareto front should be valid and serialize correctly."""
    result = MultiObjectiveResult(pareto_front=[], n_trials=0, neighbor_consistency=0.0)
    d = result.to_dict()
    assert d["pareto_front"] == []
    assert d["n_trials"] == 0


# ---------------------------------------------------------------------------
# Neighbor consistency logic — tested directly on Pareto front structures.
# The full multi_objective_optimize function requires Optuna + alpha
# discovery infrastructure; this isolates the consistency computation.
# ---------------------------------------------------------------------------


def _compute_neighbor_consistency(pareto_front: list[dict], first_param: str) -> float:
    """Reimplementation of the consistency logic from bayesian_opt.py for unit testing."""
    if len(pareto_front) < 2:
        return 0.0
    sorted_front = sorted(pareto_front, key=lambda e: e["params"].get(first_param, 0.0))
    consistent_pairs = 0
    n_pairs = len(sorted_front) - 1
    for i in range(n_pairs):
        a = sorted_front[i]
        b = sorted_front[i + 1]
        sharpe_improved = b["sharpe_oos"] > a["sharpe_oos"]
        drawdown_worsened = b["abs_drawdown"] >= a["abs_drawdown"]
        turnover_worsened = b["turnover_dist"] >= a["turnover_dist"]
        if sharpe_improved and (drawdown_worsened or turnover_worsened):
            consistent_pairs += 1
        elif not sharpe_improved and (not drawdown_worsened or not turnover_worsened):
            consistent_pairs += 1
    return consistent_pairs / n_pairs if n_pairs > 0 else 0.0


def test_neighbor_consistency_all_consistent() -> None:
    """All adjacent pairs showing Sharpe-up / drawdown-up trade-offs = 100% consistent."""
    front = [
        {"params": {"p": 0.1}, "sharpe_oos": 1.0, "abs_drawdown": 0.1, "turnover_dist": 0.0},
        {"params": {"p": 0.2}, "sharpe_oos": 1.5, "abs_drawdown": 0.2, "turnover_dist": 0.0},
        {"params": {"p": 0.3}, "sharpe_oos": 2.0, "abs_drawdown": 0.3, "turnover_dist": 0.0},
    ]
    nc = _compute_neighbor_consistency(front, "p")
    assert nc == 1.0


def test_neighbor_consistency_none_consistent() -> None:
    """When Sharpe improves AND both other objectives strictly improve, consistency = 0."""
    front = [
        # Sharpe: 1.0→1.5 (improved), drawdown: 0.3→0.1 (not worsened),
        # turnover: 0.5→0.1 (not worsened, strictly lower). No trade-off → inconsistent.
        {"params": {"p": 0.1}, "sharpe_oos": 1.0, "abs_drawdown": 0.3, "turnover_dist": 0.5},
        {"params": {"p": 0.2}, "sharpe_oos": 1.5, "abs_drawdown": 0.1, "turnover_dist": 0.1},
    ]
    nc = _compute_neighbor_consistency(front, "p")
    # sharpe_improved=True, drawdown_worsened=False (0.1 < 0.3), turnover_worsened=False (0.1 < 0.5)
    # Condition: True AND (False OR False) = False → NOT consistent → nc = 0.0
    assert nc == 0.0


def test_neighbor_consistency_single_entry() -> None:
    """Single-element Pareto front should return 0.0."""
    front = [{"params": {"p": 0.1}, "sharpe_oos": 2.0, "abs_drawdown": 0.2, "turnover_dist": 0.0}]
    nc = _compute_neighbor_consistency(front, "p")
    assert nc == 0.0


def test_neighbor_consistency_empty_front() -> None:
    """Empty Pareto front should return 0.0 without raising."""
    nc = _compute_neighbor_consistency([], "p")
    assert nc == 0.0


def test_neighbor_consistency_mixed() -> None:
    """Mixed pairs: first consistent, second inconsistent → 0.5."""
    front = [
        # pair 0→1: Sharpe up, drawdown up (worsened), turnover strictly lower (not worsened)
        # → sharpe_improved=True AND (drawdown_worsened=True OR turnover_worsened=False)
        # → True AND True → consistent
        {"params": {"p": 0.1}, "sharpe_oos": 1.0, "abs_drawdown": 0.1, "turnover_dist": 0.5},
        {"params": {"p": 0.2}, "sharpe_oos": 1.5, "abs_drawdown": 0.2, "turnover_dist": 0.1},
        # pair 1→2: Sharpe up, drawdown strictly down (not worsened), turnover strictly down (not worsened)
        # → sharpe_improved=True AND (drawdown_worsened=False OR turnover_worsened=False)
        # → True AND False → inconsistent
        {"params": {"p": 0.3}, "sharpe_oos": 2.0, "abs_drawdown": 0.05, "turnover_dist": 0.0},
    ]
    nc = _compute_neighbor_consistency(front, "p")
    assert nc == 0.5


# ---------------------------------------------------------------------------
# BayesianOptConfig (shared config used by multi-objective path)
# ---------------------------------------------------------------------------


def test_bayesian_opt_config_defaults() -> None:
    """BayesianOptConfig should apply sensible defaults."""
    cfg = BayesianOptConfig(alpha_id="test_alpha", data_paths=["/tmp/data.npy"])
    assert cfg.n_trials == 30
    assert cfg.is_oos_split == 0.7
    assert cfg.latency_profile_id == "shioaji_sim_p95_v2026-03-04"
    assert isinstance(cfg.param_space, dict)
    assert "signal_threshold" in cfg.param_space


def test_bayesian_opt_config_custom_param_space() -> None:
    """Custom param_space should override the default."""
    custom_space = {"my_param": (0.01, 0.99, False)}
    cfg = BayesianOptConfig(
        alpha_id="test_alpha",
        data_paths=["/tmp/data.npy"],
        param_space=custom_space,
    )
    assert cfg.param_space == custom_space


def test_bayesian_opt_config_is_frozen() -> None:
    """BayesianOptConfig should be immutable."""
    cfg = BayesianOptConfig(alpha_id="a", data_paths=[])
    with pytest.raises(Exception):
        cfg.n_trials = 999  # type: ignore[misc]
