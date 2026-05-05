"""Slice D T8 — hand-rolled recursive-descent parser for the alpha DSL.

Minimum grammar — enough to express R47-style multiplicative formulas
such as ``d1_pe_entropy * d2_queue_survival * d3_mfg_inventory``::

    formula := expr
    expr    := term (('+' | '-') term)*
    term    := factor (('*') factor)*
    factor  := unary | primary
    unary   := ('+' | '-') factor
    primary := IDENT | NUMBER | '(' expr ')'

    IDENT   := [a-zA-Z_][a-zA-Z_0-9]*
    NUMBER  := digits ('.' digits)?

Operator precedence (lowest to highest): ``+`` / ``-`` < ``*`` < unary
``+`` / ``-`` < primary.  Binary operators are left-associative.

Safety: this parser is hand-written; it never calls :func:`eval`,
:func:`exec`, :func:`compile`, or :func:`ast.parse`.  The only escape is
:class:`DSLSyntaxError` for malformed input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


class DSLSyntaxError(ValueError):
    """Raised when the input formula cannot be parsed."""


# ---------------------------------------------------------------------------
# AST nodes (frozen + slots dataclasses, structural equality for tests).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Identifier:
    name: str


@dataclass(frozen=True, slots=True)
class Literal:
    value: float


@dataclass(frozen=True, slots=True)
class UnaryOp:
    op: str
    operand: "Node"


@dataclass(frozen=True, slots=True)
class BinOp:
    op: str
    left: "Node"
    right: "Node"


Node = Union[Identifier, Literal, UnaryOp, BinOp]


# ---------------------------------------------------------------------------
# Tokenizer.
# ---------------------------------------------------------------------------


# Token kinds.
_TOK_IDENT = "IDENT"
_TOK_NUMBER = "NUMBER"
_TOK_PLUS = "+"
_TOK_MINUS = "-"
_TOK_STAR = "*"
_TOK_LPAREN = "("
_TOK_RPAREN = ")"
_TOK_EOF = "EOF"


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    value: str
    pos: int


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "+-*()":
            kind = {
                "+": _TOK_PLUS,
                "-": _TOK_MINUS,
                "*": _TOK_STAR,
                "(": _TOK_LPAREN,
                ")": _TOK_RPAREN,
            }[ch]
            tokens.append(_Token(kind, ch, i))
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < n and (text[i].isalnum() or text[i] == "_"):
                i += 1
            tokens.append(_Token(_TOK_IDENT, text[start:i], start))
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            start = i
            seen_dot = False
            while i < n and (text[i].isdigit() or text[i] == "."):
                if text[i] == ".":
                    if seen_dot:
                        raise DSLSyntaxError(
                            f"Malformed number at position {start}: "
                            f"multiple '.' in {text[start:i + 1]!r}"
                        )
                    seen_dot = True
                i += 1
            tokens.append(_Token(_TOK_NUMBER, text[start:i], start))
            continue
        raise DSLSyntaxError(f"Unexpected character {ch!r} at position {i}")
    tokens.append(_Token(_TOK_EOF, "", n))
    return tokens


# ---------------------------------------------------------------------------
# Recursive-descent parser.
# ---------------------------------------------------------------------------


class _Parser:
    """Hand-written recursive-descent parser.

    The state is just (tokens, cursor); each grammar non-terminal is a
    method that returns a :class:`Node` and advances the cursor.
    """

    __slots__ = ("_tokens", "_pos")

    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens
        self._pos = 0

    # -- token helpers ----------------------------------------------------

    def _peek(self) -> _Token:
        return self._tokens[self._pos]

    def _advance(self) -> _Token:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    # -- grammar ----------------------------------------------------------

    def parse_formula(self) -> Node:
        if self._peek().kind == _TOK_EOF:
            raise DSLSyntaxError("Empty formula")
        node = self._parse_expr()
        eof = self._peek()
        if eof.kind != _TOK_EOF:
            raise DSLSyntaxError(
                f"Unexpected trailing token {eof.value!r} at position {eof.pos}"
            )
        return node

    def _parse_expr(self) -> Node:
        node = self._parse_term()
        while self._peek().kind in (_TOK_PLUS, _TOK_MINUS):
            op = self._advance().value
            right = self._parse_term()
            node = BinOp(op, node, right)
        return node

    def _parse_term(self) -> Node:
        node = self._parse_factor()
        while self._peek().kind == _TOK_STAR:
            op = self._advance().value
            right = self._parse_factor()
            node = BinOp(op, node, right)
        return node

    def _parse_factor(self) -> Node:
        tok = self._peek()
        if tok.kind in (_TOK_PLUS, _TOK_MINUS):
            op = self._advance().value
            operand = self._parse_factor()
            return UnaryOp(op, operand)
        return self._parse_primary()

    def _parse_primary(self) -> Node:
        tok = self._peek()
        if tok.kind == _TOK_IDENT:
            self._advance()
            return Identifier(tok.value)
        if tok.kind == _TOK_NUMBER:
            self._advance()
            try:
                value = float(tok.value)
            except ValueError as exc:  # pragma: no cover - tokenizer guards.
                raise DSLSyntaxError(
                    f"Invalid number {tok.value!r} at position {tok.pos}"
                ) from exc
            return Literal(value)
        if tok.kind == _TOK_LPAREN:
            self._advance()
            node = self._parse_expr()
            close = self._peek()
            if close.kind != _TOK_RPAREN:
                raise DSLSyntaxError(
                    f"Unmatched '(' — expected ')' at position {close.pos}, "
                    f"got {close.kind!r}"
                )
            self._advance()
            return node
        raise DSLSyntaxError(
            f"Unexpected token {tok.kind!r} ({tok.value!r}) at position {tok.pos}"
        )


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def parse(formula: str) -> Node:
    """Parse a DSL formula into an AST node.

    Raises
    ------
    DSLSyntaxError
        If the input is empty, contains an unknown character, or is not
        well-formed under the grammar above.
    """

    if not isinstance(formula, str):
        raise DSLSyntaxError(f"Expected str, got {type(formula).__name__}")
    tokens = _tokenize(formula)
    parser = _Parser(tokens)
    return parser.parse_formula()


__all__ = [
    "BinOp",
    "DSLSyntaxError",
    "Identifier",
    "Literal",
    "Node",
    "UnaryOp",
    "parse",
]
