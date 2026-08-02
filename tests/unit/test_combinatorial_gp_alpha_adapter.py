from __future__ import annotations

import numpy as np
import pytest

from hft_platform.contracts.alpha import AlphaManifest, AlphaProtocol
from research.combinatorial.expression_lang import compile_expression
from research.combinatorial.gp_alpha_adapter import (
    GPCompiledAlpha,
    max_window_for_expression,
    required_history_by_variable,
)


def _manifest(formula: str = "x") -> AlphaManifest:
    return AlphaManifest(
        alpha_id="zz_gp_adapter_test",
        hypothesis="test",
        formula=formula,
        paper_refs=(),
        data_fields=(),
        complexity="O(1)",
    )


def _synthetic_series(n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(loc=0.0, scale=3.0, size=n)


# Covers every operator in operator_library.OPERATORS at least once, plus the
# BinOp/UnaryOp forms, plus a nested-window composition.
_STREAMABLE_EXPRESSIONS = [
    "abs(x)",
    "add(x, y)",
    "div(x, y)",
    "log1p(x)",
    "mul(x, y)",
    "sign(x)",
    "(-x)",
    "ts_mean(x, 5)",
    "ts_std(x, 5)",
    "ts_sum(x, 5)",
    "ts_rank(x, 5)",
    "decay_linear(x, 5)",
    "ts_corr(x, y, 5)",
    "zscore(x, 5)",
    "ts_delta(x, 5)",
    "zscore(ts_delta(x, 3), 4)",
    "sign(ts_corr(x, y, 10))",
    "ts_corr(x, y, 1)",
    "zscore(x, 1)",
]


@pytest.mark.parametrize("expression", _STREAMABLE_EXPRESSIONS)
def test_streaming_update_matches_batch_evaluate_at_every_index(expression: str) -> None:
    n = 60
    x = _synthetic_series(n, seed=1)
    y = _synthetic_series(n, seed=2)
    features = {"x": x, "y": y}

    compiled = compile_expression(expression)
    batch_out = compiled.evaluate(features)

    alpha = GPCompiledAlpha(expression, _manifest(expression))
    stream_out = np.empty(n, dtype=np.float64)
    for i in range(n):
        payload = {name: float(arr[i]) for name, arr in features.items() if name in compiled.variables}
        stream_out[i] = alpha.update(**payload)

    np.testing.assert_allclose(stream_out, batch_out, atol=1e-7, rtol=1e-6)


def test_nested_window_buffer_sizing_matches_batch_not_naive_max_constant() -> None:
    """Regression test for a buffer-sizing bug caught during Phase 4 design:
    sizing the streaming buffer as max(w1, w2) for zscore(ts_delta(x, w1), w2)
    silently diverges from the batch-evaluated score whenever windowed
    operators nest (required history accumulates additively down the tree,
    per the gp_alpha_adapter module docstring). Hand-derived: for this
    x/w1/w2, max(w1,w2)=3 produces ~1.414; the correct additive sizing (5)
    produces ~0.805, matching the batch evaluation.
    """
    x = np.array([0.0, 0.0, 0.0, 0.0, 100.0, 0.0, 0.0, 0.0, 10.0])
    expression = "zscore(ts_delta(x, 2), 3)"
    compiled = compile_expression(expression)
    batch_out = compiled.evaluate({"x": x})

    assert max_window_for_expression(expression) == 5

    alpha = GPCompiledAlpha(expression, _manifest(expression))
    last = 0.0
    for value in x:
        last = alpha.update(x=float(value))
    assert last == pytest.approx(float(batch_out[-1]), abs=1e-8)
    assert last != pytest.approx(1.414, abs=0.05)  # the wrong, under-sized-buffer answer


def test_max_window_for_expression_pointwise_only_returns_one() -> None:
    assert max_window_for_expression("add(x, y)") == 1
    assert max_window_for_expression("abs(x)") == 1


def test_max_window_for_expression_ts_delta_uses_offset_plus_window() -> None:
    # ts_delta[i] = x[i] - x[i-w]: needs w+1 trailing samples, not w.
    assert max_window_for_expression("ts_delta(x, 5)") == 6


def test_max_window_for_expression_rolling_window_uses_window_minus_one() -> None:
    assert max_window_for_expression("ts_mean(x, 5)") == 5
    assert max_window_for_expression("ts_corr(x, y, 10)") == 10


def test_max_window_for_expression_ts_corr_and_zscore_floor_at_two() -> None:
    """Regression test: operator_library.ts_corr / rolling zscore internally
    clamp window=max(2, window) (need >=2 samples for a variance/covariance),
    unlike every other windowed op's max(1, window). Requesting window=1 must
    still size a buffer of (at least) 2, or the adapter silently streams a
    constant 0.0 instead of the batch-evaluated value (verified by hand: both
    ops' rolling implementation short-circuits to 0.0 whenever count<2).
    """
    assert max_window_for_expression("ts_corr(x, y, 1)") == 2
    assert max_window_for_expression("zscore(x, 1)") == 2


def test_max_window_for_expression_picks_max_across_variables() -> None:
    assert max_window_for_expression("add(ts_mean(x, 5), ts_sum(x, 3))") == 5


def test_required_history_is_reported_per_variable_for_feature_provenance() -> None:
    assert required_history_by_variable("add(ts_delta(x, 5), ts_mean(y, 3))") == {
        "x": 6,
        "y": 3,
    }


def test_max_window_for_expression_rejects_bare_rank() -> None:
    with pytest.raises(ValueError):
        max_window_for_expression("rank(x)")


def test_max_window_for_expression_rejects_single_arg_zscore() -> None:
    with pytest.raises(ValueError):
        max_window_for_expression("zscore(x)")


def test_max_window_for_expression_rejects_rank_nested_inside_other_ops() -> None:
    with pytest.raises(ValueError):
        max_window_for_expression("sign(rank(ts_sum(x, 5)))")


def test_gp_compiled_alpha_conforms_to_alpha_protocol() -> None:
    alpha = GPCompiledAlpha("add(x, y)", _manifest())
    assert isinstance(alpha, AlphaProtocol)


def test_gp_compiled_alpha_construction_rejects_noncausal_expression() -> None:
    with pytest.raises(ValueError):
        GPCompiledAlpha("rank(x)", _manifest())


def test_reset_clears_state_to_match_a_fresh_instance() -> None:
    alpha = GPCompiledAlpha("ts_mean(x, 3)", _manifest())
    alpha.update(x=1.0)
    alpha.update(x=2.0)
    alpha.reset()
    assert alpha.get_signal() == 0.0

    fresh = GPCompiledAlpha("ts_mean(x, 3)", _manifest())
    reset_value = alpha.update(x=5.0)
    fresh_value = fresh.update(x=5.0)
    assert reset_value == pytest.approx(fresh_value)


def test_update_missing_payload_key_defaults_to_zero() -> None:
    alpha = GPCompiledAlpha("add(x, y)", _manifest())
    result = alpha.update(x=3.0)
    assert result == pytest.approx(3.0)


def test_get_signal_returns_last_update_value_without_recomputing() -> None:
    alpha = GPCompiledAlpha("add(x, y)", _manifest())
    value = alpha.update(x=2.0, y=5.0)
    assert alpha.get_signal() == pytest.approx(value)
