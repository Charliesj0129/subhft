import random

import numpy as np
import pytest

from research.combinatorial.canonical_ast import (
    OPERATOR_ARG_ROLES,
    OPERATOR_ARITY,
    ConstNode,
    canonical_hash,
    canonical_string,
    canonicalize,
    crossover,
    role_positions,
    to_typed_ast,
    unparse,
)
from research.combinatorial.expression_lang import compile_expression


def _canon(expression: str) -> str:
    return canonical_string(canonicalize(to_typed_ast(expression)))


def test_commutative_add_reorders_operands_to_same_canonical_form():
    assert _canon("add(a, b)") == _canon("add(b, a)")


def test_commutative_mul_reorders_operands_to_same_canonical_form():
    assert _canon("mul(x, y)") == _canon("mul(y, x)")


def test_non_commutative_sub_preserves_operand_order():
    assert _canon("a - b") != _canon("b - a")


def test_non_commutative_div_preserves_operand_order():
    assert _canon("div(a, b)") != _canon("div(b, a)")


def test_binop_and_call_forms_are_semantically_unified():
    assert _canon("a + b") == _canon("add(a, b)")
    assert _canon("a * b") == _canon("mul(a, b)")
    assert _canon("a / b") == _canon("div(a, b)")


def test_numeric_literal_int_and_float_normalize_identically():
    assert _canon("mul(x, 2)") == _canon("mul(x, 2.0)")


def test_whitespace_and_formatting_are_irrelevant():
    assert _canon("add( a , b )") == _canon("add(a,b)")


def test_nested_mixed_commutative_and_non_commutative_tree_is_deterministic():
    left = _canon("add(mul(2, ofi), mid - ts_delta(px, 5))")
    right = _canon("add(mid - ts_delta(px, 5), mul(ofi, 2))")
    assert left == right


def test_canonical_hash_matches_for_semantically_equivalent_expressions():
    assert canonical_hash("add(a, b)") == canonical_hash("b + a")


def test_canonical_hash_differs_for_semantically_different_expressions():
    assert canonical_hash("add(a, b)") != canonical_hash("a - b")


def test_arity_mismatch_missing_arg_raises_value_error_at_compile_time():
    with pytest.raises(ValueError):
        to_typed_ast("ts_mean(x)")


def test_arity_mismatch_extra_arg_raises_value_error_at_compile_time():
    with pytest.raises(ValueError):
        to_typed_ast("sign(x, y)")


def test_operator_arity_table_covers_every_call_form_operator():
    from research.combinatorial.operator_library import OPERATORS

    for name in OPERATORS:
        assert name in OPERATOR_ARITY


def test_malformed_syntax_raises_syntax_error_not_value_error():
    with pytest.raises(SyntaxError):
        to_typed_ast("add(x,")


def test_unary_minus_produces_neg_node_and_unary_plus_is_identity():
    assert _canon("-x") == "neg(x)"
    assert _canon("+x") == _canon("x")


# --- Phase 3: OPERATOR_ARG_ROLES / role_positions / crossover / unparse ---


def test_operator_arg_roles_table_covers_every_call_form_operator_and_sub_neg():
    from research.combinatorial.operator_library import OPERATORS

    for name in OPERATORS:
        assert name in OPERATOR_ARG_ROLES
    assert "sub" in OPERATOR_ARG_ROLES
    assert "neg" in OPERATOR_ARG_ROLES
    for name, bounds in OPERATOR_ARITY.items():
        _, hi = bounds
        assert len(OPERATOR_ARG_ROLES[name]) == hi


def test_role_positions_labels_window_arg_and_signal_args_correctly():
    tree = to_typed_ast("ts_mean(x, 5)")
    roles = role_positions(tree)

    window_values = [n.value for n in roles["window"] if isinstance(n, ConstNode)]
    assert window_values == [5.0]

    signal_names = [n.name for n in roles["signal"] if hasattr(n, "name")]
    assert "x" in signal_names


def test_unparse_roundtrips_to_semantically_identical_expression_for_every_operator():
    features = {
        "x": np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0], dtype=np.float64),
        "y": np.array([2.0, 1.0, 4.0, 3.0, 6.0, 5.0, 8.0, 7.0], dtype=np.float64),
    }
    expressions = [
        "abs(x)",
        "add(x, y)",
        "decay_linear(x, 3)",
        "div(x, y)",
        "log1p(x)",
        "mul(x, y)",
        "rank(x)",
        "sign(x)",
        "x - y",  # sub — no Call form; canonical_string would print invalid sub(x,y)
        "ts_corr(x, y, 3)",
        "ts_delta(x, 3)",
        "ts_mean(x, 3)",
        "ts_rank(x, 3)",
        "ts_std(x, 3)",
        "ts_sum(x, 3)",
        "zscore(x, 3)",
        "-x",  # neg — no Call form; canonical_string would print invalid neg(x)
    ]
    for expr in expressions:
        unparsed = unparse(to_typed_ast(expr))
        original = compile_expression(expr).evaluate(features)
        roundtripped = compile_expression(unparsed).evaluate(features)
        assert np.allclose(original, roundtripped), (expr, unparsed)


def test_unparse_negative_const_node_has_no_double_sign_and_is_valid_syntax():
    # Not reachable via to_typed_ast today (negative literals always arrive as
    # neg(...) — see test_unary_minus_produces_neg_node_and_unary_plus_is_identity)
    # but guards a future caller constructing ConstNode(-5.0) directly.
    text = unparse(ConstNode(-5.0))
    assert "--" not in text
    result = compile_expression(text).evaluate({"x": np.array([1.0, 2.0])})
    assert np.allclose(result, -5.0)


class _ScriptedRng:
    """Fake rng exposing only ``.choice`` in a fixed call sequence — lets a test pick
    an exact tree position deterministically instead of guessing seeds."""

    def __init__(self, choices):
        self._choices = list(choices)
        self._i = 0

    def choice(self, seq):
        picked = self._choices[self._i]
        self._i += 1
        return seq[picked] if isinstance(picked, int) else picked


def test_crossover_replaces_intended_occurrence_by_identity_not_leftmost_equal_node():
    # x - x (sub has no Call form — see test_unparse_roundtrips... — so this is
    # built via BinOp syntax): two structurally-equal (dataclass __eq__) but
    # distinct (is) VarNode("x") objects. A `==`-based (rather than `is`-based)
    # replacement would silently hit the leftmost occurrence regardless of which
    # node was actually selected — undetectable with a commutative op (add), so
    # this deliberately uses non-commutative sub.
    tree = to_typed_ast("x - x")
    left, right = tree.args
    assert left == right
    assert left is not right

    # Call sequence inside crossover(): 1) common_roles -> "signal", 2) target from
    # roles_a["signal"] = [root, left_x, right_x] -> index 2 (right_x, NOT left_x),
    # 3) donor from roles_b["signal"] = [root_b, y_node, z_node] -> index 1 (y_node).
    rng = _ScriptedRng(["signal", 2, 1])
    child = crossover("x - x", "y - z", rng)

    assert child == "(x - y)"  # right_x replaced -> x stays left, y takes right's place
    result = compile_expression(child).evaluate({"x": np.array([1.0]), "y": np.array([2.0])})
    assert np.allclose(result, -1.0)  # x - y = 1 - 2; would be y - x = 1.0 if leftmost were hit


def test_crossover_never_produces_uncaught_typeerror_across_many_seeds():
    features = {
        "x": np.linspace(1.0, 10.0, 12),
        "y": np.linspace(2.0, 20.0, 12),
    }
    templates = [
        "zscore(ts_delta(x, 5), 5)",
        "sign(ts_delta(x, 3))",
        "rank(ts_sum(x, 10))",
        "sign(ts_corr(x, y, 5))",
    ]
    successes = 0
    for seed in range(200):
        rng = random.Random(seed)
        expr_a = rng.choice(templates)
        expr_b = rng.choice(templates)
        try:
            child = crossover(expr_a, expr_b, rng)
        except ValueError:
            continue
        try:
            compiled = compile_expression(child)
        except ValueError:
            continue
        compiled.evaluate(features)  # must never raise TypeError
        successes += 1
    assert successes > 0
