"""Slice D T9 — DSL tree-walk interpreter.

Turns an AST produced by :mod:`hft_platform.alpha.dsl.parser` into a
callable that evaluates the formula against a feature dictionary::

    fn = compile_ast(parse("a * b + c"))
    result = fn({"a": np.array(...), "b": np.array(...), "c": np.array(...)})

Codex §13.1 safety constraints — this module deliberately avoids:

* the dynamic-execution functions ``eval``, ``exec``, and the built-in
  named after the source-code-to-bytecode translator;
* dynamic attribute lookup;
* dynamic imports.

Dispatch is performed with ``isinstance`` on the four AST node types.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np

from .parser import BinOp, Identifier, Literal, Node, UnaryOp


class DSLNameError(KeyError):
    """Raised when a formula references an identifier missing from the
    feature dictionary supplied at evaluation time."""


_Features = dict[str, np.ndarray]
_CompiledFn = Callable[[_Features], np.ndarray]


def _evaluate(node: Node, features: _Features) -> Any:
    """Tree-walk evaluator (no dynamic execution).

    Returns numpy arrays for identifier lookups and broadcastable
    arithmetic results; returns plain ``float`` for literals (numpy
    handles broadcasting against arrays).
    """

    if isinstance(node, Literal):
        return node.value

    if isinstance(node, Identifier):
        try:
            return features[node.name]
        except KeyError as exc:
            raise DSLNameError(node.name) from exc

    if isinstance(node, UnaryOp):
        operand = _evaluate(node.operand, features)
        if node.op == "+":
            return +operand
        if node.op == "-":
            return -operand
        raise TypeError(f"Unknown unary operator: {node.op!r}")

    if isinstance(node, BinOp):
        left = _evaluate(node.left, features)
        right = _evaluate(node.right, features)
        if node.op == "+":
            return left + right
        if node.op == "-":
            return left - right
        if node.op == "*":
            return left * right
        raise TypeError(f"Unknown binary operator: {node.op!r}")

    raise TypeError(f"Unknown AST node type: {type(node).__name__}")


def compile_ast(node: Node) -> _CompiledFn:
    """Return a callable that evaluates ``node`` against a feature dict.

    Parameters
    ----------
    node :
        An AST root produced by :func:`parse` (or constructed directly
        from :class:`Identifier` / :class:`Literal` / :class:`UnaryOp` /
        :class:`BinOp`).

    Returns
    -------
    callable
        ``fn(features: dict[str, np.ndarray]) -> np.ndarray`` — evaluates
        the formula by tree-walking ``node`` and applying numpy
        arithmetic.

    Raises
    ------
    TypeError
        If ``node`` is not a recognised AST node type.
    DSLNameError
        Raised by the returned callable if a referenced identifier is
        missing from the feature dictionary.
    """

    if not isinstance(node, (Identifier, Literal, UnaryOp, BinOp)):
        raise TypeError(
            f"compile_ast expected a DSL AST node, got "
            f"{type(node).__name__}"
        )

    def _run(features: _Features) -> np.ndarray:
        result = _evaluate(node, features)
        # Coerce scalar literals to numpy so the return type matches the
        # documented contract (np.ndarray).  Operations against feature
        # arrays already produce ndarray; only bare-Literal roots need
        # coercion.
        if isinstance(result, np.ndarray):
            return result
        return np.asarray(result)

    return _run


__all__ = ["DSLNameError", "compile_ast"]
