"""Grammar parse/reject matrix (spec §7): calls, kwargs, strings, /, compare."""

from __future__ import annotations

import pytest

from research.candidate_loop.grammar import (
    BinOp,
    Call,
    Compare,
    FormulaSyntaxError,
    Identifier,
    Number,
    String,
    UnaryOp,
    call_depth,
    node_count,
    parse,
)


class TestParseAccepts:
    def test_call_with_positional_args(self) -> None:
        node = parse("depth_sum('bid', 3)")
        assert node == Call("depth_sum", (String("bid"), Number(3.0)), ())

    def test_call_with_kwargs(self) -> None:
        node = parse("zscore(x, window='2000_events')")
        assert node == Call("zscore", (Identifier("x"),), (("window", String("2000_events")),))

    def test_bare_identifier_side_argument(self) -> None:
        node = parse("depth_sum(bid, 2)")
        assert node == Call("depth_sum", (Identifier("bid"), Number(2.0)), ())

    def test_double_quoted_string(self) -> None:
        assert parse('"500ms"') == String("500ms")

    def test_division_binds_tighter_than_addition(self) -> None:
        node = parse("a + b / c")
        assert node == BinOp("+", Identifier("a"), BinOp("/", Identifier("b"), Identifier("c")))

    def test_unary_minus_on_call(self) -> None:
        node = parse("-mid_price()")
        assert node == UnaryOp("-", Call("mid_price", (), ()))

    def test_negative_number_argument(self) -> None:
        node = parse("clip(x, -3, 3)")
        assert node.args[1] == UnaryOp("-", Number(3.0))

    def test_nested_calls(self) -> None:
        node = parse("ema(zscore(book_imbalance(3)), '100_events')")
        assert isinstance(node, Call) and node.name == "ema"
        inner = node.args[0]
        assert isinstance(inner, Call) and inner.name == "zscore"

    def test_parenthesized_arithmetic(self) -> None:
        node = parse("(a + b) * c")
        assert node == BinOp("*", BinOp("+", Identifier("a"), Identifier("b")), Identifier("c"))

    def test_compare_allowed_in_regime_context(self) -> None:
        node = parse("spread_ticks() <= 2", allow_compare=True)
        assert node == Compare("<=", Call("spread_ticks", (), ()), Number(2.0))

    @pytest.mark.parametrize("op", ["<", ">", "<=", ">=", "=="])
    def test_all_comparison_operators(self, op: str) -> None:
        node = parse(f"a {op} 1", allow_compare=True)
        assert isinstance(node, Compare) and node.op == op


class TestParseRejects:
    def test_empty_formula(self) -> None:
        with pytest.raises(FormulaSyntaxError):
            parse("")

    def test_unbalanced_paren(self) -> None:
        with pytest.raises(FormulaSyntaxError):
            parse("zscore(imb")

    def test_unterminated_string(self) -> None:
        with pytest.raises(FormulaSyntaxError):
            parse("zscore(x, window='2000_events)")

    def test_compare_rejected_outside_regime_context(self) -> None:
        with pytest.raises(FormulaSyntaxError, match="regime_filter"):
            parse("spread_ticks() <= 2")

    def test_chained_comparison_rejected(self) -> None:
        with pytest.raises(FormulaSyntaxError):
            parse("1 < spread_ticks() < 3", allow_compare=True)

    def test_positional_after_keyword_argument(self) -> None:
        with pytest.raises(FormulaSyntaxError, match="keyword"):
            parse("clip(x, lo=-1, 1)")

    def test_trailing_garbage(self) -> None:
        with pytest.raises(FormulaSyntaxError, match="trailing"):
            parse("a + b c")

    def test_unknown_character(self) -> None:
        with pytest.raises(FormulaSyntaxError):
            parse("a @ b")

    def test_multiple_dots_in_number(self) -> None:
        with pytest.raises(FormulaSyntaxError):
            parse("1.2.3")

    def test_lone_assignment_outside_call(self) -> None:
        with pytest.raises(FormulaSyntaxError):
            parse("a = b")


class TestComplexityHelpers:
    def test_node_count_counts_every_node(self) -> None:
        # Call(zscore) + Identifier(x) + String window = 3
        assert node_count(parse("zscore(x, window='2000_events')")) == 3

    def test_call_depth_of_nested_transforms(self) -> None:
        node = parse("zscore(ema(clip(zscore(book_imbalance(2)), -1, 1), '50_events'))")
        assert call_depth(node) == 5

    def test_call_depth_zero_for_arithmetic(self) -> None:
        assert call_depth(parse("a + b * c")) == 0

    def test_call_depth_of_sibling_calls_is_one(self) -> None:
        assert call_depth(parse("mid_price() - microprice()")) == 1
