from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from research.combinatorial.operator_library import OPERATORS


_RAW_PRICE_TOKENS = ("price", "px")
_TRANSFORM_TOKENS = ("delta", "diff", "return", "spread", "ratio", "mid")
_ALLOWED_BIN_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
_ALLOWED_UNARY_OPS = (ast.UAdd, ast.USub)


@dataclass(frozen=True)
class CompiledExpression:
    expression: str
    tree: ast.Expression
    max_depth: int
    variables: tuple[str, ...]

    def evaluate(self, features: Mapping[str, Any]) -> np.ndarray:
        context = {str(k): np.asarray(v, dtype=np.float64).reshape(-1) for k, v in features.items()}
        if not context:
            raise ValueError("No features supplied for expression evaluation")

        base_len = min(arr.size for arr in context.values())
        if base_len <= 0:
            raise ValueError("Feature arrays are empty")
        trimmed = {name: arr[:base_len] for name, arr in context.items()}

        raw = _eval_node(self.tree.body, trimmed, base_len)
        out = np.asarray(raw, dtype=np.float64)
        if out.ndim == 0:
            out = np.full(base_len, float(out), dtype=np.float64)
        out = out.reshape(-1)
        if out.size != base_len:
            out = out[:base_len]
        return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)


def compile_expression(
    expression: str,
    *,
    max_depth: int = 3,
    forbid_raw_price_levels: bool = True,
) -> CompiledExpression:
    validated = validate_expression(
        expression,
        max_depth=max_depth,
        forbid_raw_price_levels=forbid_raw_price_levels,
    )
    tree = ast.parse(expression, mode="eval")
    return CompiledExpression(
        expression=expression,
        tree=tree,
        max_depth=validated["depth"],
        variables=tuple(validated["variables"]),
    )


def validate_expression(
    expression: str,
    *,
    max_depth: int = 3,
    forbid_raw_price_levels: bool = True,
) -> dict[str, Any]:
    tree = ast.parse(expression, mode="eval")
    names, depth = _validate_tree(tree)
    if depth > max_depth:
        raise ValueError(f"Expression depth {depth} exceeds max_depth={max_depth}")
    if forbid_raw_price_levels:
        bad = [name for name in names if _looks_like_raw_price(name)]
        if bad:
            raise ValueError(f"Raw price level variable is not allowed: {sorted(set(bad))}")
    return {"depth": depth, "variables": sorted(set(names))}


def _validate_tree(tree: ast.Expression) -> tuple[list[str], int]:
    names: list[str] = []

    def walk(node: ast.AST, depth: int) -> int:
        max_seen = depth
        if isinstance(node, ast.Expression):
            return walk(node.body, depth)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return depth
            raise ValueError(f"Unsupported constant type: {type(node.value).__name__}")
        if isinstance(node, ast.Name):
            names.append(node.id)
            return depth
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("Only direct function calls are allowed")
            if node.func.id not in OPERATORS:
                raise ValueError(f"Unsupported operator: {node.func.id}")
            for arg in node.args:
                max_seen = max(max_seen, walk(arg, depth + 1))
            return max_seen
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, _ALLOWED_BIN_OPS):
                raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")
            max_seen = max(max_seen, walk(node.left, depth + 1))
            max_seen = max(max_seen, walk(node.right, depth + 1))
            return max_seen
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, _ALLOWED_UNARY_OPS):
                raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
            return max(max_seen, walk(node.operand, depth + 1))
        raise ValueError(f"Unsupported syntax node: {type(node).__name__}")

    depth = walk(tree, 1)
    return names, depth


def _eval_node(node: ast.AST, context: Mapping[str, np.ndarray], base_len: int) -> Any:
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in context:
            raise KeyError(f"Unknown feature name in expression: {node.id}")
        return context[node.id]
    if isinstance(node, ast.UnaryOp):
        val = _eval_node(node.operand, context, base_len)
        if isinstance(node.op, ast.USub):
            return -val
        if isinstance(node.op, ast.UAdd):
            return val
        raise ValueError(f"Unsupported unary operator at runtime: {type(node.op).__name__}")
    if isinstance(node, ast.BinOp):
        lhs = _eval_node(node.left, context, base_len)
        rhs = _eval_node(node.right, context, base_len)
        return _eval_binop(node.op, lhs, rhs)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only direct function calls are allowed")
        name = node.func.id
        fn = OPERATORS.get(name)
        if fn is None:
            raise ValueError(f"Unsupported operator: {name}")
        args = [_eval_node(arg, context, base_len) for arg in node.args]
        return fn(*args)
    raise ValueError(f"Unsupported node at runtime: {type(node).__name__}")


def _eval_binop(op: ast.AST, lhs: Any, rhs: Any) -> Any:
    if isinstance(op, ast.Add):
        return lhs + rhs
    if isinstance(op, ast.Sub):
        return lhs - rhs
    if isinstance(op, ast.Mult):
        return lhs * rhs
    if isinstance(op, ast.Div):
        rhs_arr = np.asarray(rhs, dtype=np.float64)
        lhs_arr = np.asarray(lhs, dtype=np.float64)
        out = np.zeros(np.broadcast(lhs_arr, rhs_arr).shape, dtype=np.float64)
        np.divide(lhs_arr, rhs_arr, out=out, where=np.abs(rhs_arr) > 1e-12)
        return out
    raise ValueError(f"Unsupported binary operator at runtime: {type(op).__name__}")


def _looks_like_raw_price(name: str) -> bool:
    lower = name.lower()
    if any(token in lower for token in _TRANSFORM_TOKENS):
        return False
    return any(token in lower for token in _RAW_PRICE_TOKENS)
