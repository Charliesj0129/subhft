from __future__ import annotations

import numpy as np
import pytest

from research.combinatorial.expression_eval import evaluate_family_expression
from research.combinatorial.expression_lang import compile_expression


@pytest.mark.parametrize(
    "expression",
    [
        "decay_linear(x, 3)",
        "ts_corr(x, x, 3)",
        "ts_delta(x, 3)",
        "ts_mean(x, 3)",
        "ts_rank(x, 3)",
        "ts_std(x, 3)",
        "ts_sum(x, 3)",
        "zscore(x, 3)",
    ],
)
def test_every_windowed_operator_restarts_at_a_governed_reset(expression: str) -> None:
    values = np.asarray([1.0, 2.0, 3.0, 0.0, -1.0, -2.0])
    companion = np.asarray([1.0, 4.0, 2.0, 10.0, 0.0, 5.0])
    resets = np.asarray([True, False, False, True, False, False])
    features = {"x": values, "y": companion}

    actual = evaluate_family_expression(expression, features, resets)
    compiled = compile_expression(expression)
    first = {name: features[name][:3] for name in compiled.variables}
    second = {name: features[name][3:] for name in compiled.variables}
    expected = np.concatenate(
        (
            compiled.evaluate(first),
            compiled.evaluate(second),
        )
    )

    np.testing.assert_allclose(actual, expected)
