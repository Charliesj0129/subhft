import numpy as np

from research.combinatorial.expression_lang import compile_expression, validate_expression
from research.combinatorial.search_engine import AlphaSearchEngine


def test_compile_expression_evaluate():
    expr = compile_expression("zscore(ts_delta(ofi, 3), 5)")
    out = expr.evaluate({"ofi": np.array([0.0, 1.0, 2.0, 3.0, 2.0, 1.0], dtype=np.float64)})
    assert out.shape == (6,)
    assert np.isfinite(out).all()


def test_validate_expression_rejects_raw_price():
    try:
        validate_expression("ts_delta(price, 5)")
    except ValueError as exc:
        assert "Raw price level variable" in str(exc)
    else:
        raise AssertionError("Expected validate_expression to reject raw price feature")


def test_search_engine_random_and_template():
    features = {
        "ofi": np.array([0.0, 0.1, 0.3, -0.1, 0.2, 0.25, 0.1], dtype=np.float64),
        "bid_qty": np.array([10, 12, 11, 9, 13, 14, 13], dtype=np.float64),
    }
    returns = np.array([0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.005], dtype=np.float64)
    engine = AlphaSearchEngine(features=features, returns=returns, random_seed=7)

    random_results = engine.random_search(n_trials=8)
    assert random_results
    assert all(np.isfinite(item.score) for item in random_results)

    template_results = engine.template_sweep("zscore(ts_delta(ofi, {w}), 5)", {"w": [3, 5]})
    assert len(template_results) == 2
    assert template_results[0].score >= template_results[1].score
