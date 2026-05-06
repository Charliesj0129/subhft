"""Slice D T9 — DSL tree-walk interpreter tests.

Verifies that :func:`hft_platform.alpha.dsl.compile_ast` produces a numpy
callable that matches direct numpy arithmetic for ``+``, ``-``, ``*``,
parentheses, and unary signs.

Includes a Codex §13.1 safety-attestation test that reads ``compiler.py``
and asserts it contains no ``eval(``, ``exec(``, ``compile(``,
``__import__``, or ``getattr(`` strings — the interpreter must remain a
plain hand-written tree walk with no escape hatches.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from hft_platform.alpha.dsl import (
    BinOp,
    DSLNameError,
    Identifier,
    Literal,
    UnaryOp,
    compile_ast,
    parse,
)


def _features() -> dict[str, np.ndarray]:
    return {
        "a": np.array([1.0, 2.0, 3.0, 4.0]),
        "b": np.array([10.0, 20.0, 30.0, 40.0]),
        "c": np.array([0.5, 0.25, 0.125, 0.0625]),
    }


def test_compile_identifier_returns_feature() -> None:
    fn = compile_ast(Identifier("a"))
    feats = _features()
    np.testing.assert_array_equal(fn(feats), feats["a"])


def test_compile_literal() -> None:
    fn = compile_ast(Literal(2.0))
    out = fn(_features())
    assert float(out) == 2.0


def test_compile_addition_matches_numpy() -> None:
    fn = compile_ast(parse("a + b"))
    feats = _features()
    np.testing.assert_array_equal(fn(feats), feats["a"] + feats["b"])


def test_compile_subtraction_matches_numpy() -> None:
    fn = compile_ast(parse("a - b"))
    feats = _features()
    np.testing.assert_array_equal(fn(feats), feats["a"] - feats["b"])


def test_compile_multiplication_matches_numpy() -> None:
    fn = compile_ast(parse("a * b"))
    feats = _features()
    np.testing.assert_array_equal(fn(feats), feats["a"] * feats["b"])


def test_compile_unary_minus() -> None:
    fn = compile_ast(parse("-a"))
    feats = _features()
    np.testing.assert_array_equal(fn(feats), -feats["a"])


def test_compile_unary_plus() -> None:
    fn = compile_ast(UnaryOp("+", Identifier("a")))
    feats = _features()
    np.testing.assert_array_equal(fn(feats), +feats["a"])


def test_compile_complex_formula_matches_numpy() -> None:
    fn = compile_ast(parse("(a + b) * c"))
    feats = _features()
    np.testing.assert_array_equal(fn(feats), (feats["a"] + feats["b"]) * feats["c"])


def test_compile_precedence_matches_numpy() -> None:
    # a + b * c should respect precedence (b * c first, then a + result).
    fn = compile_ast(parse("a + b * c"))
    feats = _features()
    np.testing.assert_array_equal(fn(feats), feats["a"] + feats["b"] * feats["c"])


def test_compile_r47_canonical_formula() -> None:
    fn = compile_ast(parse("a * b * c"))
    feats = _features()
    np.testing.assert_array_equal(fn(feats), feats["a"] * feats["b"] * feats["c"])


def test_compile_literal_in_expression() -> None:
    fn = compile_ast(parse("2 * a + 1"))
    feats = _features()
    np.testing.assert_array_equal(fn(feats), 2.0 * feats["a"] + 1.0)


def test_compile_missing_feature_raises_dsl_name_error() -> None:
    fn = compile_ast(parse("a + missing_feature"))
    with pytest.raises(DSLNameError) as exc:
        fn({"a": np.array([1.0])})
    # The missing identifier name should be carried in the exception.
    assert "missing_feature" in str(exc.value)


def test_dsl_name_error_is_key_error_subclass() -> None:
    assert issubclass(DSLNameError, KeyError)


def test_compile_unknown_node_type_raises() -> None:
    """Defensive guard: feeding a non-AST object should not silently work."""

    class Bogus:  # local marker; not an AST node
        pass

    with pytest.raises(TypeError):
        compile_ast(Bogus())  # type: ignore[arg-type]


def test_compile_no_eval_no_exec_no_compile_no_getattr_no_import() -> None:
    """Codex §13.1 safety attestation.

    The compiled interpreter must remain a small hand-written tree walk
    with no dynamic-execution escape hatches.  Read the source of
    ``compiler.py`` and assert none of the dangerous patterns appear.
    """

    src_path = Path(__file__).resolve().parents[3] / "src/hft_platform/alpha/dsl/compiler.py"
    assert src_path.exists(), f"compiler.py not found at {src_path}"
    source = src_path.read_text(encoding="utf-8")

    forbidden = ("eval(", "exec(", "compile(", "__import__", "getattr(")
    found = [needle for needle in forbidden if needle in source]
    assert not found, f"compiler.py must not contain dynamic-execution escape hatches; found: {found}"


def test_compile_binop_smoke_test() -> None:
    fn = compile_ast(parse("a * b"))
    out = fn(
        {
            "a": np.array([1.0, 2.0, 3.0]),
            "b": np.array([10.0, 20.0, 30.0]),
        }
    )
    np.testing.assert_array_equal(out, np.array([10.0, 40.0, 90.0]))


def test_compile_binop_node_directly() -> None:
    # Build the AST directly without going through parse(), to verify
    # compile_ast does not depend on parser-only metadata.
    node = BinOp("+", Identifier("a"), Identifier("b"))
    fn = compile_ast(node)
    feats = {"a": np.array([1.0]), "b": np.array([2.0])}
    np.testing.assert_array_equal(fn(feats), np.array([3.0]))
