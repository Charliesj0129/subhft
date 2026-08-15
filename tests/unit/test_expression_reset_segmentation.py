"""Windowed operators must not read samples from the previous reset segment.

``evaluate_family_expression`` used to decide whether to segment by scanning the
expression *text* for three operator names. Every other windowed operator in
``OPERATORS`` silently took the whole-array path, so its rolling window reached
back across a contract/session boundary. These tests pin the boundary itself:
the value at the first index after a reset must depend only on samples from the
new segment.

Measured against the pre-fix code, ``ts_mean``, ``ts_std``, ``decay_linear`` and
``rank`` all returned a blended value at the boundary — e.g. ``ts_mean`` read
0.4 where the segment alone gives 1.0.
"""

from __future__ import annotations

import numpy as np
import pytest

from research.combinatorial.expression_eval import evaluate_family_expression
from research.combinatorial.expression_lang import compile_expression
from research.combinatorial.operator_library import (
    CROSS_SAMPLE_OPERATORS,
    ELEMENTWISE_OPERATORS,
    OPERATORS,
)

WINDOW = 5
SEGMENT_LEN = 12
RESET_INDEX = SEGMENT_LEN

CROSS_SAMPLE_EXPRESSIONS = [
    f"ts_mean(vol_imbalance, {WINDOW})",
    f"ts_std(vol_imbalance, {WINDOW})",
    f"ts_sum(vol_imbalance, {WINDOW})",
    f"ts_rank(vol_imbalance, {WINDOW})",
    f"decay_linear(vol_imbalance, {WINDOW})",
    f"ts_delta(vol_imbalance, {WINDOW})",
    f"zscore(vol_imbalance, {WINDOW})",
    "rank(vol_imbalance)",
]


def _two_segment_inputs() -> tuple[dict[str, np.ndarray], np.ndarray]:
    """Two segments whose sample values do not overlap at all.

    The first segment sits at 2.0 and the second at 1.0, so any window reaching
    across the boundary lands strictly between the two — a value the correctly
    segmented evaluation can never produce. Both levels are non-zero on purpose:
    a zero first segment makes ``ts_sum`` agree by coincidence and hides the bug.
    """
    values = np.concatenate([np.full(SEGMENT_LEN, 2.0), np.full(SEGMENT_LEN, 1.0)])
    resets = np.zeros(values.size, dtype=bool)
    resets[0] = True
    resets[RESET_INDEX] = True
    return {"vol_imbalance": values}, resets


def _evaluated_per_segment(expression: str, features: dict[str, np.ndarray]) -> np.ndarray:
    """Reference answer: evaluate each segment on its own array, independently."""
    compiled = compile_expression(expression, max_depth=3)
    out = np.zeros(SEGMENT_LEN * 2, dtype=np.float64)
    out[:RESET_INDEX] = compiled.evaluate({k: v[:RESET_INDEX] for k, v in features.items()})
    out[RESET_INDEX:] = compiled.evaluate({k: v[RESET_INDEX:] for k, v in features.items()})
    return out


@pytest.mark.parametrize("expression", CROSS_SAMPLE_EXPRESSIONS)
def test_cross_sample_operator_does_not_consume_samples_across_a_reset(expression: str) -> None:
    features, resets = _two_segment_inputs()

    actual = evaluate_family_expression(expression, features, resets)

    np.testing.assert_allclose(actual, _evaluated_per_segment(expression, features))


@pytest.mark.parametrize("expression", CROSS_SAMPLE_EXPRESSIONS)
def test_segmented_answer_differs_from_the_whole_array_answer(expression: str) -> None:
    """Guard the guard.

    A correctness test that passes because both paths happen to agree on this
    fixture proves nothing, so assert the fixture actually separates them.
    """
    features, resets = _two_segment_inputs()
    whole_array = compile_expression(expression, max_depth=3).evaluate(features)

    actual = evaluate_family_expression(expression, features, resets)

    assert not np.allclose(actual, whole_array)


def test_expression_without_cross_sample_operators_still_takes_the_fast_path() -> None:
    """Segmenting everything would be correct but needlessly slow."""
    features, resets = _two_segment_inputs()
    expression = "sign(vol_imbalance) + log1p(vol_imbalance)"

    actual = evaluate_family_expression(expression, features, resets)

    np.testing.assert_allclose(actual, _evaluated_per_segment(expression, features))


def test_nested_cross_sample_operator_is_still_segmented() -> None:
    """The operator that leaks may sit under an elementwise one."""
    features, resets = _two_segment_inputs()
    expression = f"rank(ts_mean(vol_imbalance, {WINDOW}))"

    actual = evaluate_family_expression(expression, features, resets)

    np.testing.assert_allclose(actual, _evaluated_per_segment(expression, features))
    assert not np.allclose(actual, compile_expression(expression, max_depth=3).evaluate(features))


def test_every_operator_is_classified_as_elementwise_or_cross_sample() -> None:
    """A newly added operator must not default into the whole-array fast path."""
    assert ELEMENTWISE_OPERATORS | CROSS_SAMPLE_OPERATORS == set(OPERATORS)
    assert not (ELEMENTWISE_OPERATORS & CROSS_SAMPLE_OPERATORS)
