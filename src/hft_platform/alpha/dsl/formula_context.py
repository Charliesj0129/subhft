"""Slice D T10 — DSL round-trip and manifest binding helpers.

Two surface helpers used by the alpha-factory CLI to handle DSL formulas
at the manifest boundary:

* :func:`round_trip(formula)` — parses the formula and unparses it back
  to a canonical string.  Idempotent: the second call is a fixpoint.
* :func:`bind_to_manifest(formula, manifest)` — returns a new
  :class:`AlphaManifest` whose ``dsl_formula`` field is the canonical
  form of ``formula``, and raises :class:`DSLNameError` if the formula
  references identifiers absent from ``manifest.data_fields``.

Canonicalisation rules
----------------------

* One space around binary operators (``a * b``, ``a + b``, ``a - b``).
* No space after unary (``-a``, ``+a``).
* Parentheses only where precedence requires them.
* Numeric literals: integers print without trailing ``.0``; other
  values use :func:`repr` (shortest round-trippable form).
"""

from __future__ import annotations

import dataclasses
import math

from research.registry.schemas import AlphaManifest

from .compiler import DSLNameError
from .parser import BinOp, Identifier, Literal, Node, UnaryOp, parse

# Operator precedence table.  Larger values bind tighter.
_PRECEDENCE: dict[str, int] = {
    "+": 1,
    "-": 1,
    "*": 2,
}

# Unary precedence — kept separate so the unparser can reason about
# "tighter than every binary op" without polluting _PRECEDENCE.
_UNARY_PRECEDENCE = 3


def _format_literal(value: float) -> str:
    """Render a numeric literal using the shortest correct form."""

    if math.isnan(value) or math.isinf(value):
        return repr(value)
    if value == int(value):
        # Integer-valued float: drop the trailing ``.0``.
        return str(int(value))
    return repr(value)


def _unparse(node: Node, parent_prec: int = 0) -> str:
    """Pretty-print ``node`` as a canonical formula string.

    ``parent_prec`` is the precedence of the surrounding operator.
    Parentheses are inserted when the current node's precedence is less
    than the parent's (so we keep parens that are required and drop the
    rest).
    """

    if isinstance(node, Literal):
        return _format_literal(node.value)

    if isinstance(node, Identifier):
        return node.name

    if isinstance(node, UnaryOp):
        # Unary binds tighter than every binary op, so we pass
        # _UNARY_PRECEDENCE to the operand context.  The "no space after
        # unary" rule means we render as e.g. ``-a`` (not ``- a``).
        operand = _unparse(node.operand, _UNARY_PRECEDENCE)
        return f"{node.op}{operand}"

    if isinstance(node, BinOp):
        prec = _PRECEDENCE[node.op]
        # Left associativity: the left operand only needs >= our
        # precedence, but the right operand needs strictly higher to
        # avoid parens.
        left = _unparse(node.left, prec)
        right = _unparse(node.right, prec + 1)
        text = f"{left} {node.op} {right}"
        if prec < parent_prec:
            return f"({text})"
        return text

    raise TypeError(f"Unknown AST node type: {type(node).__name__}")


def round_trip(formula: str) -> str:
    """Parse ``formula`` and return its canonical string form.

    Calling :func:`round_trip` on its own output is a fixpoint::

        canonical = round_trip(formula)
        assert round_trip(canonical) == canonical
    """

    node = parse(formula)
    return _unparse(node)


def _collect_identifiers(node: Node, out: set[str]) -> None:
    if isinstance(node, Identifier):
        out.add(node.name)
        return
    if isinstance(node, Literal):
        return
    if isinstance(node, UnaryOp):
        _collect_identifiers(node.operand, out)
        return
    if isinstance(node, BinOp):
        _collect_identifiers(node.left, out)
        _collect_identifiers(node.right, out)
        return
    raise TypeError(f"Unknown AST node type: {type(node).__name__}")


def bind_to_manifest(formula: str, manifest: AlphaManifest) -> AlphaManifest:
    """Validate ``formula`` against ``manifest.data_fields`` and bind it.

    Returns a new :class:`AlphaManifest` whose ``dsl_formula`` field is
    the canonicalised form of ``formula``.  The input ``manifest`` is
    not mutated (it is frozen anyway).

    Raises
    ------
    DSLSyntaxError
        Propagated from :func:`parse` if the formula is malformed.
    DSLNameError
        If the formula references an identifier that is not declared in
        ``manifest.data_fields``.
    """

    node = parse(formula)
    referenced: set[str] = set()
    _collect_identifiers(node, referenced)
    declared = set(manifest.data_fields)
    missing = referenced - declared
    if missing:
        # Surface a single name in the message for stable logging; the
        # full set is included so callers can enumerate.
        first = sorted(missing)[0]
        raise DSLNameError(
            f"Formula references identifier(s) not in manifest.data_fields: {sorted(missing)}; first missing={first!r}"
        )
    canonical = _unparse(node)
    return dataclasses.replace(manifest, dsl_formula=canonical)


__all__ = ["bind_to_manifest", "round_trip"]
