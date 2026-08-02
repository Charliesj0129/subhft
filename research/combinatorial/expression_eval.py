"""Family-agnostic GP expression evaluation shared by every mining family.

Extracted from ``research/combinatorial/smma.py``'s ``evaluate_smma_expression``,
which had no SMMA-specific logic in its body. Every mining family (smma,
bidask, tick, ...) evaluates candidate expressions the same way: windowed
operators must never see samples from across a reset-segment boundary.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np

from research.combinatorial.canonical_ast import OPERATOR_ARG_ROLES, OpNode, to_typed_ast


def _uses_windowed_operator(expression: str) -> bool:
    def visit(node: object) -> bool:
        if not isinstance(node, OpNode):
            return False
        roles = OPERATOR_ARG_ROLES[node.op][: len(node.args)]
        return "window" in roles or any(visit(child) for child in node.args)

    return visit(to_typed_ast(expression))


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
    if not _uses_windowed_operator(expression):
        return compiled.evaluate(relevant)

    out: np.ndarray = np.zeros(resets.size, dtype=np.float64)
    starts = [0, *(int(index) for index in np.flatnonzero(resets[1:]) + 1)]
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else resets.size
        segment = {name: values[start:end] for name, values in relevant.items()}
        out[start:end] = compiled.evaluate(segment)
    return out
