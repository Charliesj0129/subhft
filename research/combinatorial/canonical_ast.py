"""Typed AST, canonicalization, and semantic-identity hashing for alpha expressions.

Builds on the already-validated grammar in ``expression_lang.py`` (``Name``,
``Constant``, ``Call`` into ``OPERATORS``, ``BinOp`` in ``{Add,Sub,Mult,Div}``,
``UnaryOp`` in ``{UAdd,USub}``) and adds two things that grammar doesn't have:

1. **Typed arity checking** (``OPERATOR_ARITY``): today
   ``expression_lang._validate_tree`` only checks that a called name is a
   known operator, not that the call has the right number of arguments — a
   bad-arity expression compiles successfully and only fails at runtime as an
   uncaught ``TypeError`` inside the operator callable. ``to_typed_ast``
   catches this at compile time as a clean ``ValueError``.
2. **Canonicalization** (``canonicalize`` / ``canonical_string`` /
   ``canonical_hash``): semantically-equivalent expressions collapse to the
   same identity — commutative operators (``add``, ``mul``) get their operands
   sorted, ``BinOp`` unifies with its equivalent ``Call`` form (``a + b`` ==
   ``add(a, b)``; verified numerically identical against ``operator_library``,
   including ``div``'s ``eps=1e-12`` safe-division guard), and numeric
   literals normalize to one textual form (``5`` == ``5.0``). Deliberately
   does NOT do deeper algebraic simplification (distribution, identity-element
   elimination, constant folding across sub-expressions) — those risk
   silently changing evaluated semantics in floating-point edge cases.

``research/combinatorial/ledger.py`` uses ``canonical_hash`` as the semantic
component of trial/candidate identity, replacing Phase 1's purely-textual
normalization.
"""

from __future__ import annotations

import ast
import hashlib
import random
from dataclasses import dataclass

from research.combinatorial.expression_lang import _validate_tree


@dataclass(frozen=True)
class ConstNode:
    value: float


@dataclass(frozen=True)
class VarNode:
    name: str


@dataclass(frozen=True)
class OpNode:
    op: str
    args: tuple["Node", ...]


Node = ConstNode | VarNode | OpNode

_COMMUTATIVE_OPS = frozenset({"add", "mul"})

_BINOP_NAMES: dict[type[ast.AST], str] = {
    ast.Add: "add",
    ast.Sub: "sub",
    ast.Mult: "mul",
    ast.Div: "div",
}

# (min_args, max_args) per operator, mirroring operator_library.OPERATORS'
# actual positional-arg signatures (keyword-only out=/eps= excluded). "sub"
# has no OPERATORS callable (no Call form exists) but is reachable via BinOp.
OPERATOR_ARITY: dict[str, tuple[int, int]] = {
    "abs": (1, 1),
    "add": (2, 2),
    "decay_linear": (2, 2),
    "div": (2, 2),
    "log1p": (1, 1),
    "mul": (2, 2),
    "rank": (1, 1),
    "sign": (1, 1),
    "sub": (2, 2),
    "ts_corr": (3, 3),
    "ts_delta": (2, 2),
    "ts_mean": (2, 2),
    "ts_rank": (2, 2),
    "ts_std": (2, 2),
    "ts_sum": (2, 2),
    "zscore": (1, 2),
}

# Per-positional-arg role for every operator: "signal" (array-valued, may be
# any subtree) or "window" (consumed via int(window) at runtime by
# operator_library — must stay a scalar constant). Covers OPERATOR_ARITY's 16
# keys plus "neg" (reachable only via UnaryOp, never Call, so it has no arity
# entry). zscore's 1-arg call form takes roles[:1], a valid prefix slice.
OPERATOR_ARG_ROLES: dict[str, tuple[str, ...]] = {
    "abs": ("signal",),
    "add": ("signal", "signal"),
    "decay_linear": ("signal", "window"),
    "div": ("signal", "signal"),
    "log1p": ("signal",),
    "mul": ("signal", "signal"),
    "neg": ("signal",),
    "rank": ("signal",),
    "sign": ("signal",),
    "sub": ("signal", "signal"),
    "ts_corr": ("signal", "signal", "window"),
    "ts_delta": ("signal", "window"),
    "ts_mean": ("signal", "window"),
    "ts_rank": ("signal", "window"),
    "ts_std": ("signal", "window"),
    "ts_sum": ("signal", "window"),
    "zscore": ("signal", "window"),
}

_INFIX_OPS: dict[str, str] = {"add": "+", "sub": "-", "mul": "*", "div": "/"}


def to_typed_ast(expression: str) -> Node:
    """Parse, grammar-validate, and arity-check *expression* into a typed node tree.

    Raises ``SyntaxError`` on malformed Python syntax, ``ValueError`` on
    grammar violations (via ``expression_lang._validate_tree``) or arity
    mismatches.
    """
    tree = ast.parse(expression, mode="eval")
    _validate_tree(tree)
    return _convert(tree.body)


def canonicalize(node: Node) -> Node:
    """Recursively rewrite *node* into canonical form (bottom-up)."""
    if isinstance(node, OpNode):
        args = tuple(canonicalize(a) for a in node.args)
        if node.op in _COMMUTATIVE_OPS:
            args = tuple(sorted(args, key=canonical_string))
        return OpNode(node.op, args)
    return node


def canonical_string(node: Node) -> str:
    """Deterministic S-expression form of *node*, e.g. ``add(mul(2,x),y)``."""
    if isinstance(node, ConstNode):
        return format(node.value, ".12g")
    if isinstance(node, VarNode):
        return node.name
    if isinstance(node, OpNode):
        return f"{node.op}({','.join(canonical_string(a) for a in node.args)})"
    raise TypeError(f"Unknown node type: {type(node).__name__}")  # pragma: no cover - exhaustive union


def canonical_hash(expression: str) -> str:
    """sha256 of the canonical form of *expression* — the semantic identity."""
    node = canonicalize(to_typed_ast(expression))
    return hashlib.sha256(canonical_string(node).encode("utf-8")).hexdigest()


def unparse(node: Node) -> str:
    """Serialize a typed node tree back into a valid, re-parseable expression string.

    Unlike ``canonical_string`` (hash-identity only — prints ``sub``/``neg`` in
    invalid Call-form since those names aren't in ``OPERATORS``), this always
    emits syntax that ``compile_expression``/``to_typed_ast`` can consume:
    ``add``/``sub``/``mul``/``div`` as parenthesized infix, ``neg`` as a
    parenthesized prefix ``-``, every other operator in Call-form.
    """
    if isinstance(node, ConstNode):
        return repr(float(node.value))
    if isinstance(node, VarNode):
        return node.name
    if isinstance(node, OpNode):
        if node.op == "neg":
            return f"(-{unparse(node.args[0])})"
        symbol = _INFIX_OPS.get(node.op)
        if symbol is not None:
            left, right = node.args
            return f"({unparse(left)} {symbol} {unparse(right)})"
        return f"{node.op}({', '.join(unparse(a) for a in node.args)})"
    raise TypeError(f"Unknown node type: {type(node).__name__}")  # pragma: no cover - exhaustive union


def role_positions(node: Node) -> dict[str, list[Node]]:
    """Map role (``"signal"``/``"window"``) -> node objects at that role, by identity.

    The root is always role ``"signal"`` (the whole expression's output is the
    scored array; a root can never resolve to ``"window"``). Must be called on
    a **freshly-parsed** tree (i.e. straight from ``to_typed_ast``, never from
    ``canonicalize()``, which rebuilds ``OpNode`` wrappers for commutative
    operands and would break identity-based node selection downstream).
    """
    out: dict[str, list[Node]] = {}
    _collect_role_positions(node, "signal", out)
    return out


def _collect_role_positions(node: Node, role: str, out: dict[str, list[Node]]) -> None:
    out.setdefault(role, []).append(node)
    if isinstance(node, OpNode):
        arg_roles = OPERATOR_ARG_ROLES.get(node.op)
        if arg_roles is None:
            raise ValueError(f"No arg-role definition for operator: {node.op}")
        for child, child_role in zip(node.args, arg_roles[: len(node.args)], strict=True):
            _collect_role_positions(child, child_role, out)


def crossover(expression_a: str, expression_b: str, rng: random.Random) -> str:
    """Typed, role-aware subtree crossover: swap one same-role subtree of *expression_a*
    for a same-role subtree of *expression_b*, returning the resulting expression string.

    Both expressions are parsed fresh (never canonicalized — see ``role_positions``)
    so that Python object identity (``is``) safely identifies one specific tree
    position even when the tree contains structurally-equal duplicate subtrees
    (e.g. ``add(x, x)``). Only same-role swaps are permitted ("signal" <-> "signal",
    "window" <-> "window") so a window arg — consumed via ``int(window)`` at
    runtime by ``operator_library`` — can never be replaced by a non-constant
    array-valued subtree.

    Raises ``ValueError`` if the two parents share no common role. In practice
    this is unreachable from any tree produced by ``to_typed_ast`` — the root
    is always assigned role "signal" (see ``role_positions``), so any two
    valid parsed trees always share at least that role — but it's kept as a
    fail-closed guard rather than an unchecked ``rng.choice([])`` on an empty
    sequence. Callers should still be prepared to handle it (e.g. by falling
    back to another child-generation strategy) in case that invariant ever
    changes.
    """
    tree_a = to_typed_ast(expression_a)
    tree_b = to_typed_ast(expression_b)
    roles_a = role_positions(tree_a)
    roles_b = role_positions(tree_b)
    common_roles = sorted(set(roles_a) & set(roles_b))
    if not common_roles:
        raise ValueError("No common crossover role between parent expressions")

    role = rng.choice(common_roles)
    target = rng.choice(roles_a[role])
    donor = rng.choice(roles_b[role])
    child_tree = _replace_by_identity(tree_a, target, donor)
    return unparse(child_tree)


def _replace_by_identity(node: Node, target: Node, replacement: Node) -> Node:
    if node is target:
        return replacement
    if isinstance(node, OpNode):
        return OpNode(node.op, tuple(_replace_by_identity(a, target, replacement) for a in node.args))
    return node


def _convert(node: ast.AST) -> Node:
    if isinstance(node, ast.Constant):
        return ConstNode(float(node.value))
    if isinstance(node, ast.Name):
        return VarNode(node.id)
    if isinstance(node, ast.UnaryOp):
        operand = _convert(node.operand)
        if isinstance(node.op, ast.USub):
            return OpNode("neg", (operand,))
        return operand  # UAdd is a semantic no-op, same as in _eval_node
    if isinstance(node, ast.BinOp):
        op_name = _BINOP_NAMES.get(type(node.op))
        if op_name is None:
            raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")
        return OpNode(op_name, (_convert(node.left), _convert(node.right)))
    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)  # guaranteed by _validate_tree
        name = node.func.id
        args = tuple(_convert(arg) for arg in node.args)
        _check_arity(name, len(args))
        return OpNode(name, args)
    raise ValueError(f"Unsupported syntax node: {type(node).__name__}")  # pragma: no cover - grammar already validated


def _check_arity(name: str, count: int) -> None:
    bounds = OPERATOR_ARITY.get(name)
    if bounds is None:
        raise ValueError(f"No arity definition for operator: {name}")
    lo, hi = bounds
    if not (lo <= count <= hi):
        raise ValueError(f"Operator '{name}' expects {lo}-{hi} args, got {count}")
