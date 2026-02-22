import numpy as np

from research.registry.correlation_tracker import CorrelationTracker
from research.registry.pool_optimizer import AlphaPoolOptimizer


def test_correlation_tracker_and_optimizer():
    signals = {
        "a": np.array([0.1, 0.2, 0.3, 0.1, 0.0], dtype=np.float64),
        "b": np.array([0.12, 0.22, 0.28, 0.08, -0.01], dtype=np.float64),
        "c": np.array([-0.2, -0.1, 0.0, 0.2, 0.3], dtype=np.float64),
    }
    tracker = CorrelationTracker()
    payload = tracker.compute_matrix(signals, sample_step=1)
    assert payload["alpha_ids"] == ["a", "b", "c"]
    assert "spearman_matrix" in payload

    redundant = tracker.flag_redundant(payload, threshold=0.7, metric="pearson")
    assert redundant

    optimizer = AlphaPoolOptimizer()
    weights = optimizer.optimize(signals=signals, method="ridge")
    assert set(weights) == {"a", "b", "c"}
    assert np.isclose(sum(abs(v) for v in weights.values()), 1.0)

    marginal = optimizer.marginal_test(
        new_signal=np.array([0.0, 0.1, 0.2, 0.1, 0.0], dtype=np.float64),
        existing_signals={"a": signals["a"], "b": signals["b"]},
        method="equal_weight",
        min_uplift=-1.0,
    )
    assert "uplift" in marginal
