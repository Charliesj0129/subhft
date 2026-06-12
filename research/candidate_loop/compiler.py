"""Evaluate validator-canonical ASTs against a per-day Panel (spec §7/§10).

Consumes :class:`~research.candidate_loop.validator.ValidCandidate` ASTs —
feature-inlined, argument-canonicalized (all-positional Call args, windows
normalized to ``'N_events'`` / ``'<ns>ns'``) — so the dispatch here is a plain
isinstance tree-walk with no argument binding logic.

* ``evaluate_signal`` returns a float64 series (one value per panel row);
* ``evaluate_regime`` returns a boolean row mask (NaN comparisons are False);
* ``evaluate_label`` computes the ``future_mid_return`` label for the
  candidate's raw horizon spec.

Anything structurally impossible after validation (leftover identifier,
``future_mid_return`` in expression position, a Compare outside the regime
root) raises :class:`CompileError` — defensive, not a death reason.
"""

from __future__ import annotations

import numpy as np

from research.candidate_loop import primitives
from research.candidate_loop.grammar import (
    BinOp,
    Call,
    Compare,
    Identifier,
    Node,
    Number,
    String,
    UnaryOp,
)
from research.candidate_loop.primitives import Columns, parse_canonical_window
from research.candidate_loop.schema import LABEL_PRIMITIVE


class CompileError(RuntimeError):
    """A canonical AST could not be evaluated (validator contract breach)."""


def evaluate_signal(node: Node, cols: Columns) -> np.ndarray:
    """Evaluate an inlined signal AST to a float64 series over panel rows."""
    return _as_series(_eval(node, cols), _n_rows(cols))


def evaluate_regime(node: Node, cols: Columns) -> np.ndarray:
    """Evaluate a canonical regime Compare to a boolean row mask."""
    if not isinstance(node, Compare):
        raise CompileError(f"regime root must be a Compare, got {type(node).__name__}")
    n = _n_rows(cols)
    left = _as_series(_eval(node.left, cols), n)
    right = _as_series(_eval(node.right, cols), n)
    with np.errstate(invalid="ignore"):
        if node.op == "<=":
            return left <= right
        if node.op == ">=":
            return left >= right
        if node.op == "<":
            return left < right
        if node.op == ">":
            return left > right
        if node.op == "==":
            return left == right
    raise CompileError(f"unknown comparison operator {node.op!r}")  # pragma: no cover


def evaluate_label(cols: Columns, horizon: str) -> np.ndarray:
    """``future_mid_return`` label for a candidate horizon ('Nms'/'Ns'/'N_events')."""
    return primitives.future_mid_return(cols, parse_canonical_window(horizon))


def _n_rows(cols: Columns) -> int:
    return int(cols["local_ts"].size)


def _as_series(value: np.ndarray | float, n: int) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if arr.ndim == 0:
        return np.full(n, float(arr))
    return arr


def _eval(node: Node, cols: Columns) -> np.ndarray | float:
    if isinstance(node, Number):
        return node.value
    if isinstance(node, UnaryOp):
        operand = _eval(node.operand, cols)
        return -np.asarray(operand, dtype=np.float64) if isinstance(operand, np.ndarray) else -operand
    if isinstance(node, BinOp):
        left = _eval(node.left, cols)
        right = _eval(node.right, cols)
        if node.op == "+":
            return left + right
        if node.op == "-":
            return left - right
        if node.op == "*":
            return left * right
        if node.op == "/":
            return primitives.safe_divide(left, right)
        raise CompileError(f"unknown operator {node.op!r}")  # pragma: no cover
    if isinstance(node, Call):
        return _eval_call(node, cols)
    if isinstance(node, Identifier):
        raise CompileError(f"unresolved identifier {node.name!r} — features must be inlined by the validator")
    if isinstance(node, String):
        raise CompileError(f"string literal {node.value!r} in expression position")
    if isinstance(node, Compare):
        raise CompileError("comparison is only valid as the regime_filter root")
    raise CompileError(f"unknown node type {type(node).__name__}")  # pragma: no cover


def _eval_call(call: Call, cols: Columns) -> np.ndarray:
    name = call.name
    if name == "mid_price":
        return primitives.mid_price(cols)
    if name == "spread_ticks":
        return primitives.spread_ticks(cols)
    if name == "microprice":
        return primitives.microprice(cols)
    if name == "depth_sum":
        return primitives.depth_sum(cols, _string_arg(call, 0), _int_arg(call, 1))
    if name == "book_imbalance":
        return primitives.book_imbalance(cols, _int_arg(call, 0))
    if name == "depth_delta":
        window = parse_canonical_window(_string_arg(call, 2))
        return primitives.depth_delta(cols, _string_arg(call, 0), _int_arg(call, 1), window)
    if name == "trade_imbalance":
        return primitives.trade_imbalance(cols, parse_canonical_window(_string_arg(call, 0)))
    if name == LABEL_PRIMITIVE:
        raise CompileError(f"{LABEL_PRIMITIVE} is label-only and may not appear in formulas")
    if name in ("zscore", "negative_zscore"):
        x = _as_series(_eval(call.args[0], cols), _n_rows(cols))
        window = parse_canonical_window(_string_arg(call, 1))
        z = primitives.rolling_zscore(x, cols["local_ts"], window)
        return -z if name == "negative_zscore" else z
    if name == "ema":
        x = _as_series(_eval(call.args[0], cols), _n_rows(cols))
        return primitives.ema(x, cols["local_ts"], parse_canonical_window(_string_arg(call, 1)))
    if name == "clip":
        clip_x = _eval(call.args[0], cols)
        return primitives.clip(clip_x, _number_arg(call, 1), _number_arg(call, 2))
    raise CompileError(f"{name!r} is not a prim_v1 primitive/transform")


def _string_arg(call: Call, idx: int) -> str:
    arg = call.args[idx]
    if not isinstance(arg, String):
        raise CompileError(f"{call.name} arg {idx} must be a canonical String, got {type(arg).__name__}")
    return arg.value


def _int_arg(call: Call, idx: int) -> int:
    arg = call.args[idx]
    if not isinstance(arg, Number):
        raise CompileError(f"{call.name} arg {idx} must be a canonical Number, got {type(arg).__name__}")
    return int(arg.value)


def _number_arg(call: Call, idx: int) -> float:
    arg = call.args[idx]
    if not isinstance(arg, Number):
        raise CompileError(f"{call.name} arg {idx} must be a canonical Number, got {type(arg).__name__}")
    return arg.value


__all__ = [
    "CompileError",
    "evaluate_label",
    "evaluate_regime",
    "evaluate_signal",
]
