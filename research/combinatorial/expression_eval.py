"""Family-agnostic GP expression evaluation shared by every mining family.

Extracted from ``research/combinatorial/smma.py``'s ``evaluate_smma_expression``,
which had no SMMA-specific logic in its body. Every mining family (smma,
bidask, tick, ...) evaluates candidate expressions the same way: windowed
operators must never see samples from across a reset-segment boundary.
"""

from __future__ import annotations

import ast
from typing import Mapping, Sequence

import numpy as np

from research.combinatorial.operator_library import ELEMENTWISE_OPERATORS


def evaluate_family_expression(
    expression: str,
    features: Mapping[str, np.ndarray],
    reset_mask: Sequence[bool] | np.ndarray,
) -> np.ndarray:
    """Evaluate a GP expression without allowing rolling windows to cross resets."""
    from research.combinatorial.expression_lang import compile_expression

    compiled = compile_expression(expression, max_depth=3)
    relevant = {name: np.asarray(features[name], dtype=np.float64).reshape(-1) for name in compiled.variables}
    sizes = {values.size for values in relevant.values()}
    resets = np.asarray(reset_mask, dtype=np.bool_).reshape(-1)
    if len(sizes) != 1 or (sizes and next(iter(sizes)) != resets.size):
        raise ValueError("expression features and reset mask must have identical lengths")
    if not relevant:
        raise ValueError("expressions must reference at least one family feature")
    if not _reads_across_samples(compiled.tree):
        return compiled.evaluate(relevant)

    out: np.ndarray = np.zeros(resets.size, dtype=np.float64)
    starts = [0, *(int(index) for index in np.flatnonzero(resets[1:]) + 1)]
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else resets.size
        segment = {name: values[start:end] for name, values in relevant.items()}
        out[start:end] = compiled.evaluate(segment)
    return out


def _reads_across_samples(tree: ast.Expression) -> bool:
    """Does any operator in *tree* read a position other than its own?

    Decided from the compiled AST rather than by scanning the expression text,
    and fail-closed: an operator is assumed to read across samples unless it is
    named in ``ELEMENTWISE_OPERATORS``. Segmenting an expression that did not
    need it costs one extra array slice; not segmenting one that did lets the
    previous contract's samples into this contract's values.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in ELEMENTWISE_OPERATORS:
                return True
    return False
