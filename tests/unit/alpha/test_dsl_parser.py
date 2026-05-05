"""Slice D T8 — DSL recursive-descent parser tests.

Verifies that the hand-rolled parser produces the expected AST for the
minimum grammar needed to express R47-style multiplicative formulas like
`d1_pe_entropy * d2_queue_survival * d3_mfg_inventory`.

Grammar:
    formula := expr
    expr    := term (('+' | '-') term)*
    term    := factor (('*') factor)*
    factor  := unary | primary
    unary   := ('+' | '-') factor
    primary := IDENT | NUMBER | '(' expr ')'
"""

from __future__ import annotations

import pytest

from hft_platform.alpha.dsl import (
    BinOp,
    DSLSyntaxError,
    Identifier,
    Literal,
    UnaryOp,
    parse,
)


def test_parse_identifier() -> None:
    assert parse("a") == Identifier("a")


def test_parse_literal_int() -> None:
    assert parse("42") == Literal(42.0)


def test_parse_literal_float() -> None:
    assert parse("3.14") == Literal(3.14)


def test_parse_binop_mul() -> None:
    assert parse("a * b") == BinOp("*", Identifier("a"), Identifier("b"))


def test_parse_precedence_star_over_plus() -> None:
    # a + b * c should parse as a + (b * c)
    assert parse("a + b * c") == BinOp(
        "+",
        Identifier("a"),
        BinOp("*", Identifier("b"), Identifier("c")),
    )


def test_parse_parens_override_precedence() -> None:
    # (a + b) * c
    assert parse("(a + b) * c") == BinOp(
        "*",
        BinOp("+", Identifier("a"), Identifier("b")),
        Identifier("c"),
    )


def test_parse_unary_minus() -> None:
    assert parse("-a") == UnaryOp("-", Identifier("a"))


def test_parse_unary_plus() -> None:
    assert parse("+a") == UnaryOp("+", Identifier("a"))


def test_parse_subtraction_left_associative() -> None:
    # a - b - c => (a - b) - c
    assert parse("a - b - c") == BinOp(
        "-",
        BinOp("-", Identifier("a"), Identifier("b")),
        Identifier("c"),
    )


def test_parse_multiplication_left_associative() -> None:
    # a * b * c => (a * b) * c
    assert parse("a * b * c") == BinOp(
        "*",
        BinOp("*", Identifier("a"), Identifier("b")),
        Identifier("c"),
    )


def test_parse_r47_canonical_formula() -> None:
    formula = "d1_pe_entropy * d2_queue_survival * d3_mfg_inventory"
    expected = BinOp(
        "*",
        BinOp(
            "*",
            Identifier("d1_pe_entropy"),
            Identifier("d2_queue_survival"),
        ),
        Identifier("d3_mfg_inventory"),
    )
    assert parse(formula) == expected


def test_parse_nested_parens() -> None:
    assert parse("((a))") == Identifier("a")


def test_parse_identifier_with_underscores_and_digits() -> None:
    assert parse("d1_pe_entropy") == Identifier("d1_pe_entropy")
    assert parse("_x9") == Identifier("_x9")


@pytest.mark.parametrize(
    "bad_input",
    [
        "",
        "   ",
        "(a + b",
        "a + b)",
        "a b",  # trailing garbage / two adjacent primaries
        "a +",
        "* b",
        "a @ b",  # unknown char
        "a..b",
        "1.2.3",
    ],
)
def test_parse_syntax_errors(bad_input: str) -> None:
    with pytest.raises(DSLSyntaxError):
        parse(bad_input)


def test_dsl_syntax_error_is_value_error_subclass() -> None:
    assert issubclass(DSLSyntaxError, ValueError)
