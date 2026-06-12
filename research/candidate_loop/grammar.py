"""Formula grammar for the candidate loop DSL (spec §7, prim_v1).

Hand-rolled recursive-descent parser, modeled on
``src/hft_platform/alpha/dsl/parser.py`` and extended with function calls
(positional + keyword args), string literals, division, and a single
top-level comparison (regime filters only)::

    formula  := compare
    compare  := expr (CMP expr)?          # CMP in <= >= < > ==, top level only
    expr     := term (('+' | '-') term)*
    term     := factor (('*' | '/') factor)*
    factor   := ('+' | '-') factor | primary
    primary  := call | IDENT | NUMBER | STRING | '(' expr ')'
    call     := IDENT '(' arglist? ')'
    arglist  := arg (',' arg)*
    arg      := IDENT '=' expr | expr

Comparisons are only legal at the very top level (parens re-enter ``expr``),
so ``a < b < c`` cannot parse — "exactly one comparison" is enforced by the
grammar itself.  Callers pass ``allow_compare=True`` only for
``regime_filter``.

Safety: never calls ``eval``/``exec``/``compile``/``ast.parse``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union


class FormulaSyntaxError(ValueError):
    """Raised when a formula cannot be parsed (maps to FORMULA_PARSE_ERROR)."""


# ---------------------------------------------------------------------------
# AST nodes (frozen dataclasses, structural equality for tests).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Identifier:
    name: str


@dataclass(frozen=True, slots=True)
class Number:
    value: float


@dataclass(frozen=True, slots=True)
class String:
    value: str


@dataclass(frozen=True, slots=True)
class UnaryOp:
    op: str
    operand: "Node"


@dataclass(frozen=True, slots=True)
class BinOp:
    op: str
    left: "Node"
    right: "Node"


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple["Node", ...] = ()
    kwargs: tuple[tuple[str, "Node"], ...] = field(default=())


@dataclass(frozen=True, slots=True)
class Compare:
    op: str
    left: "Node"
    right: "Node"


Node = Union[Identifier, Number, String, UnaryOp, BinOp, Call, Compare]

COMPARE_OPS = ("<=", ">=", "==", "<", ">")


# ---------------------------------------------------------------------------
# Tokenizer.
# ---------------------------------------------------------------------------

_TOK_IDENT = "IDENT"
_TOK_NUMBER = "NUMBER"
_TOK_STRING = "STRING"
_TOK_OP = "OP"  # + - * /
_TOK_CMP = "CMP"  # <= >= < > ==
_TOK_LPAREN = "("
_TOK_RPAREN = ")"
_TOK_COMMA = ","
_TOK_ASSIGN = "="
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
        if ch in "+-*/":
            tokens.append(_Token(_TOK_OP, ch, i))
            i += 1
            continue
        if ch == "(":
            tokens.append(_Token(_TOK_LPAREN, ch, i))
            i += 1
            continue
        if ch == ")":
            tokens.append(_Token(_TOK_RPAREN, ch, i))
            i += 1
            continue
        if ch == ",":
            tokens.append(_Token(_TOK_COMMA, ch, i))
            i += 1
            continue
        if ch in "<>=":
            two = text[i : i + 2]
            if two in ("<=", ">=", "=="):
                tokens.append(_Token(_TOK_CMP, two, i))
                i += 2
                continue
            if ch in "<>":
                tokens.append(_Token(_TOK_CMP, ch, i))
                i += 1
                continue
            tokens.append(_Token(_TOK_ASSIGN, "=", i))
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            start = i
            i += 1
            buf: list[str] = []
            while i < n and text[i] != quote:
                buf.append(text[i])
                i += 1
            if i >= n:
                raise FormulaSyntaxError(f"Unterminated string starting at position {start}")
            i += 1  # closing quote
            tokens.append(_Token(_TOK_STRING, "".join(buf), start))
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
                        raise FormulaSyntaxError(
                            f"Malformed number at position {start}: multiple '.' in {text[start : i + 1]!r}"
                        )
                    seen_dot = True
                i += 1
            tokens.append(_Token(_TOK_NUMBER, text[start:i], start))
            continue
        raise FormulaSyntaxError(f"Unexpected character {ch!r} at position {i}")
    tokens.append(_Token(_TOK_EOF, "", n))
    return tokens


# ---------------------------------------------------------------------------
# Recursive-descent parser.
# ---------------------------------------------------------------------------


class _Parser:
    __slots__ = ("_tokens", "_pos")

    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens
        self._pos = 0

    def _peek(self, ahead: int = 0) -> _Token:
        idx = min(self._pos + ahead, len(self._tokens) - 1)
        return self._tokens[idx]

    def _advance(self) -> _Token:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def parse_formula(self, allow_compare: bool) -> Node:
        if self._peek().kind == _TOK_EOF:
            raise FormulaSyntaxError("Empty formula")
        node = self._parse_expr()
        tok = self._peek()
        if tok.kind == _TOK_CMP:
            if not allow_compare:
                raise FormulaSyntaxError(
                    f"Comparison {tok.value!r} at position {tok.pos} is only allowed in regime_filter"
                )
            op = self._advance().value
            right = self._parse_expr()
            node = Compare(op, node, right)
            tok = self._peek()
        if tok.kind != _TOK_EOF:
            raise FormulaSyntaxError(f"Unexpected trailing token {tok.value!r} at position {tok.pos}")
        return node

    def _parse_expr(self) -> Node:
        node = self._parse_term()
        while self._peek().kind == _TOK_OP and self._peek().value in "+-":
            op = self._advance().value
            right = self._parse_term()
            node = BinOp(op, node, right)
        return node

    def _parse_term(self) -> Node:
        node = self._parse_factor()
        while self._peek().kind == _TOK_OP and self._peek().value in "*/":
            op = self._advance().value
            right = self._parse_factor()
            node = BinOp(op, node, right)
        return node

    def _parse_factor(self) -> Node:
        tok = self._peek()
        if tok.kind == _TOK_OP and tok.value in "+-":
            op = self._advance().value
            operand = self._parse_factor()
            return UnaryOp(op, operand)
        return self._parse_primary()

    def _parse_primary(self) -> Node:
        tok = self._peek()
        if tok.kind == _TOK_IDENT:
            if self._peek(1).kind == _TOK_LPAREN:
                return self._parse_call()
            self._advance()
            return Identifier(tok.value)
        if tok.kind == _TOK_NUMBER:
            self._advance()
            try:
                value = float(tok.value)
            except ValueError as exc:  # pragma: no cover - tokenizer guards
                raise FormulaSyntaxError(f"Invalid number {tok.value!r} at position {tok.pos}") from exc
            return Number(value)
        if tok.kind == _TOK_STRING:
            self._advance()
            return String(tok.value)
        if tok.kind == _TOK_LPAREN:
            self._advance()
            node = self._parse_expr()
            close = self._peek()
            if close.kind != _TOK_RPAREN:
                raise FormulaSyntaxError(
                    f"Unmatched '(' — expected ')' at position {close.pos}, got {close.value!r}"
                )
            self._advance()
            return node
        raise FormulaSyntaxError(f"Unexpected token {tok.value!r} at position {tok.pos}")

    def _parse_call(self) -> Node:
        name_tok = self._advance()
        self._advance()  # '('
        args: list[Node] = []
        kwargs: list[tuple[str, Node]] = []
        if self._peek().kind != _TOK_RPAREN:
            while True:
                if self._peek().kind == _TOK_IDENT and self._peek(1).kind == _TOK_ASSIGN:
                    key_tok = self._advance()
                    self._advance()  # '='
                    kwargs.append((key_tok.value, self._parse_expr()))
                else:
                    if kwargs:
                        tok = self._peek()
                        raise FormulaSyntaxError(
                            f"Positional argument after keyword argument at position {tok.pos}"
                        )
                    args.append(self._parse_expr())
                tok = self._peek()
                if tok.kind == _TOK_COMMA:
                    self._advance()
                    continue
                break
        close = self._peek()
        if close.kind != _TOK_RPAREN:
            raise FormulaSyntaxError(
                f"Expected ')' to close call {name_tok.value!r}, got {close.value!r} at position {close.pos}"
            )
        self._advance()
        return Call(name_tok.value, tuple(args), tuple(kwargs))


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def parse(formula: str, *, allow_compare: bool = False) -> Node:
    """Parse a formula into an AST.

    ``allow_compare=True`` admits exactly one top-level comparison (regime
    filters).  Raises :class:`FormulaSyntaxError` on malformed input.
    """
    if not isinstance(formula, str):
        raise FormulaSyntaxError(f"Expected str, got {type(formula).__name__}")
    return _Parser(_tokenize(formula)).parse_formula(allow_compare)


def node_count(node: Node) -> int:
    """Total AST node count (complexity limit input, spec §13)."""
    if isinstance(node, (Identifier, Number, String)):
        return 1
    if isinstance(node, UnaryOp):
        return 1 + node_count(node.operand)
    if isinstance(node, (BinOp, Compare)):
        return 1 + node_count(node.left) + node_count(node.right)
    if isinstance(node, Call):
        total = 1
        for arg in node.args:
            total += node_count(arg)
        for _, val in node.kwargs:
            total += node_count(val)
        return total
    raise TypeError(f"Unknown node type {type(node).__name__}")  # pragma: no cover


def call_depth(node: Node) -> int:
    """Maximum nesting depth of Call nodes (complexity limit input)."""
    if isinstance(node, (Identifier, Number, String)):
        return 0
    if isinstance(node, UnaryOp):
        return call_depth(node.operand)
    if isinstance(node, (BinOp, Compare)):
        return max(call_depth(node.left), call_depth(node.right))
    if isinstance(node, Call):
        inner = 0
        for arg in node.args:
            inner = max(inner, call_depth(arg))
        for _, val in node.kwargs:
            inner = max(inner, call_depth(val))
        return 1 + inner
    raise TypeError(f"Unknown node type {type(node).__name__}")  # pragma: no cover


def iter_calls(node: Node) -> list[Call]:
    """All Call nodes in the AST, pre-order."""
    out: list[Call] = []
    _collect_calls(node, out)
    return out


def _collect_calls(node: Node, out: list[Call]) -> None:
    if isinstance(node, Call):
        out.append(node)
        for arg in node.args:
            _collect_calls(arg, out)
        for _, val in node.kwargs:
            _collect_calls(val, out)
    elif isinstance(node, UnaryOp):
        _collect_calls(node.operand, out)
    elif isinstance(node, (BinOp, Compare)):
        _collect_calls(node.left, out)
        _collect_calls(node.right, out)


def iter_identifiers(node: Node) -> list[Identifier]:
    """Free identifiers in the AST (pre-order), EXCLUDING call argument
    positions — those are validated per-signature (e.g. bare ``bid``)."""
    out: list[Identifier] = []
    _collect_idents(node, out)
    return out


def _collect_idents(node: Node, out: list[Identifier]) -> None:
    if isinstance(node, Identifier):
        out.append(node)
    elif isinstance(node, UnaryOp):
        _collect_idents(node.operand, out)
    elif isinstance(node, (BinOp, Compare)):
        _collect_idents(node.left, out)
        _collect_idents(node.right, out)
    elif isinstance(node, Call):
        # Call args are intentionally NOT walked here: bare side identifiers
        # (bid/ask) are argument values, not panel/feature references.
        pass


__all__ = [
    "BinOp",
    "COMPARE_OPS",
    "Call",
    "Compare",
    "FormulaSyntaxError",
    "Identifier",
    "Node",
    "Number",
    "String",
    "UnaryOp",
    "call_depth",
    "iter_calls",
    "iter_identifiers",
    "node_count",
    "parse",
]
