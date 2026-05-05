"""Alpha DSL package — Slice D Tasks 8–10.

Hand-rolled, sandbox-safe formula language used by the alpha factory:

* :mod:`.parser` — recursive-descent parser (no ``eval`` / ``compile`` /
  ``ast.parse``).
* :mod:`.compiler` — tree-walk interpreter producing a numpy callable
  (no ``eval`` / ``exec`` / ``getattr`` / ``__import__``).
* :mod:`.formula_context` — round-trip + manifest binding helpers.

Tasks 9 and 10 extend the public surface declared here.
"""

from __future__ import annotations

from .compiler import DSLNameError, compile_ast
from .formula_context import bind_to_manifest, round_trip
from .parser import (
    BinOp,
    DSLSyntaxError,
    Identifier,
    Literal,
    Node,
    UnaryOp,
    parse,
)

__all__ = [
    "BinOp",
    "DSLNameError",
    "DSLSyntaxError",
    "Identifier",
    "Literal",
    "Node",
    "UnaryOp",
    "bind_to_manifest",
    "compile_ast",
    "parse",
    "round_trip",
]
