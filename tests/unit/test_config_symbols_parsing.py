"""Unit tests for hft_platform.config._symbols_parsing."""

from __future__ import annotations

import pytest

from hft_platform.config._symbols_parsing import (
    looks_like_filter,
    merge_tags,
    normalize_month_token,
    normalize_tags,
    parse_attrs_and_filters,
    parse_bool_value,
    parse_csv_spec,
    parse_filter_token,
    parse_kv_tokens,
    parse_list_value,
    parse_numeric_value,
    parse_range_value,
)
from hft_platform.config._symbols_types import FilterSpec, SymbolBuildResult


# ---------------------------------------------------------------------------
# parse_bool_value
# ---------------------------------------------------------------------------


class TestParseBoolValue:
    def test_truthy_string_1(self) -> None:
        assert parse_bool_value("1") is True

    def test_truthy_string_true(self) -> None:
        assert parse_bool_value("true") is True

    def test_truthy_string_yes(self) -> None:
        assert parse_bool_value("yes") is True

    def test_truthy_string_y(self) -> None:
        assert parse_bool_value("y") is True

    def test_truthy_uppercase(self) -> None:
        assert parse_bool_value("TRUE") is True

    def test_falsy_string_0(self) -> None:
        assert parse_bool_value("0") is False

    def test_falsy_string_false(self) -> None:
        assert parse_bool_value("false") is False

    def test_falsy_string_no(self) -> None:
        assert parse_bool_value("no") is False

    def test_falsy_string_n(self) -> None:
        assert parse_bool_value("n") is False

    def test_falsy_uppercase(self) -> None:
        assert parse_bool_value("NO") is False

    def test_unknown_returns_none(self) -> None:
        assert parse_bool_value("maybe") is None

    def test_empty_returns_none(self) -> None:
        assert parse_bool_value("") is None

    def test_whitespace_stripped(self) -> None:
        assert parse_bool_value("  true  ") is True

    def test_numeric_string_2_returns_none(self) -> None:
        assert parse_bool_value("2") is None


# ---------------------------------------------------------------------------
# parse_list_value
# ---------------------------------------------------------------------------


class TestParseListValue:
    def test_pipe_delimited(self) -> None:
        assert parse_list_value("a|b|c") == ["a", "b", "c"]

    def test_comma_delimited(self) -> None:
        assert parse_list_value("x,y,z") == ["x", "y", "z"]

    def test_mixed_delimiters(self) -> None:
        result = parse_list_value("a,b|c")
        assert result == ["a", "b", "c"]

    def test_strips_whitespace(self) -> None:
        assert parse_list_value(" a , b , c ") == ["a", "b", "c"]

    def test_filters_empty_tokens(self) -> None:
        assert parse_list_value("a,,b") == ["a", "b"]

    def test_empty_string_returns_empty(self) -> None:
        assert parse_list_value("") == []

    def test_single_item(self) -> None:
        assert parse_list_value("only") == ["only"]


# ---------------------------------------------------------------------------
# parse_numeric_value
# ---------------------------------------------------------------------------


class TestParseNumericValue:
    def test_integer_string(self) -> None:
        assert parse_numeric_value("42") == 42.0

    def test_float_string(self) -> None:
        assert parse_numeric_value("3.14") == pytest.approx(3.14)

    def test_with_commas(self) -> None:
        assert parse_numeric_value("1,000") == 1000.0

    def test_with_percent(self) -> None:
        assert parse_numeric_value("50%") == 50.0

    def test_negative_value(self) -> None:
        assert parse_numeric_value("-10") == -10.0

    def test_invalid_string_returns_none(self) -> None:
        assert parse_numeric_value("abc") is None

    def test_empty_string_returns_none(self) -> None:
        assert parse_numeric_value("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert parse_numeric_value("   ") is None

    def test_percent_with_decimal(self) -> None:
        assert parse_numeric_value("12.5%") == pytest.approx(12.5)


# ---------------------------------------------------------------------------
# parse_range_value
# ---------------------------------------------------------------------------


class TestParseRangeValue:
    def test_dotdot_notation(self) -> None:
        result = parse_range_value("10..20")
        assert result == (10.0, 20.0)

    def test_dash_notation(self) -> None:
        result = parse_range_value("5-15")
        assert result == (5.0, 15.0)

    def test_float_values(self) -> None:
        result = parse_range_value("1.5..3.5")
        assert result is not None
        assert result[0] == pytest.approx(1.5)
        assert result[1] == pytest.approx(3.5)

    def test_negative_start_with_dash_returns_none(self) -> None:
        # Starts with "-" → treated as plain negative number, not range
        assert parse_range_value("-5-10") is None

    def test_multiple_dashes_returns_none(self) -> None:
        # More than one dash and not starting with "-": ambiguous
        assert parse_range_value("1-2-3") is None

    def test_no_separator_returns_none(self) -> None:
        assert parse_range_value("100") is None

    def test_empty_returns_none(self) -> None:
        assert parse_range_value("") is None

    def test_invalid_low_returns_none(self) -> None:
        assert parse_range_value("abc..10") is None

    def test_invalid_high_returns_none(self) -> None:
        assert parse_range_value("10..xyz") is None

    def test_with_percent(self) -> None:
        result = parse_range_value("10%..20%")
        assert result == (10.0, 20.0)

    def test_empty_high_with_dash_returns_none(self) -> None:
        # "5-" produces empty high part
        assert parse_range_value("5-") is None


# ---------------------------------------------------------------------------
# normalize_month_token
# ---------------------------------------------------------------------------


class TestNormalizeMonthToken:
    def test_lowercase_passthrough(self) -> None:
        assert normalize_month_token("front") == "front"

    def test_uppercase_converted(self) -> None:
        assert normalize_month_token("NEAR") == "near"

    def test_strips_whitespace(self) -> None:
        assert normalize_month_token("  next  ") == "next"

    def test_empty_string(self) -> None:
        assert normalize_month_token("") == ""

    def test_none_returns_empty(self) -> None:
        assert normalize_month_token(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# normalize_tags
# ---------------------------------------------------------------------------


class TestNormalizeTags:
    def test_none_returns_empty(self) -> None:
        assert normalize_tags(None) == []

    def test_string_pipe_split(self) -> None:
        assert normalize_tags("a|b|c") == ["a", "b", "c"]

    def test_string_comma_split(self) -> None:
        assert normalize_tags("x,y,z") == ["x", "y", "z"]

    def test_list_input(self) -> None:
        assert normalize_tags(["tag1", "tag2"]) == ["tag1", "tag2"]

    def test_tuple_input(self) -> None:
        assert normalize_tags(("alpha", "beta")) == ["alpha", "beta"]

    def test_set_input_returns_list(self) -> None:
        result = normalize_tags({"only"})
        assert result == ["only"]

    def test_filters_empty_parts(self) -> None:
        result = normalize_tags("a,,b")
        assert result == ["a", "b"]

    def test_non_string_scalar(self) -> None:
        result = normalize_tags(42)
        assert result == ["42"]

    def test_strip_whitespace(self) -> None:
        result = normalize_tags(" foo , bar ")
        assert result == ["foo", "bar"]


# ---------------------------------------------------------------------------
# merge_tags
# ---------------------------------------------------------------------------


class TestMergeTags:
    def test_deduplicates_case_insensitive(self) -> None:
        result = merge_tags(["Alpha", "beta"], ["ALPHA", "gamma"])
        assert result == ["Alpha", "beta", "gamma"]

    def test_multiple_sets(self) -> None:
        result = merge_tags(["a"], ["b"], ["c"])
        assert result == ["a", "b", "c"]

    def test_empty_inputs(self) -> None:
        result = merge_tags([], [])
        assert result == []

    def test_preserves_first_case(self) -> None:
        # First occurrence is kept, not the later duplicate
        result = merge_tags(["Tag"], ["tag"])
        assert result == ["Tag"]

    def test_single_set(self) -> None:
        result = merge_tags(["x", "y", "z"])
        assert result == ["x", "y", "z"]

    def test_no_args(self) -> None:
        result = merge_tags()
        assert result == []


# ---------------------------------------------------------------------------
# looks_like_filter
# ---------------------------------------------------------------------------


class TestLooksLikeFilter:
    def test_valid_filter_key_with_equals(self) -> None:
        assert looks_like_filter("oi=100") is True

    def test_valid_filter_key_with_gte(self) -> None:
        assert looks_like_filter("price>=100") is True

    def test_valid_filter_key_with_lte(self) -> None:
        assert looks_like_filter("dte<=30") is True

    def test_valid_filter_key_with_gt(self) -> None:
        assert looks_like_filter("trades_per_min>5") is True

    def test_valid_filter_key_with_lt(self) -> None:
        assert looks_like_filter("iv_rank<50") is True

    def test_at_prefix_stripped(self) -> None:
        assert looks_like_filter("@oi>=1000") is True

    def test_unknown_key_returns_false(self) -> None:
        assert looks_like_filter("foobar=1") is False

    def test_roll_keyword_returns_false(self) -> None:
        assert looks_like_filter("roll") is False

    def test_at_roll_returns_false(self) -> None:
        assert looks_like_filter("@roll") is False

    def test_empty_string_returns_false(self) -> None:
        assert looks_like_filter("") is False

    def test_plain_symbol_returns_false(self) -> None:
        assert looks_like_filter("2330") is False


# ---------------------------------------------------------------------------
# parse_filter_token
# ---------------------------------------------------------------------------


class TestParseFilterToken:
    def _make(self) -> tuple[FilterSpec, SymbolBuildResult]:
        return FilterSpec(), SymbolBuildResult()

    def test_roll_token_sets_flag(self) -> None:
        filters, result = self._make()
        consumed = parse_filter_token("roll", filters, result, "ctx")
        assert consumed is True
        assert filters.roll is True

    def test_at_roll_token_sets_flag(self) -> None:
        filters, result = self._make()
        parse_filter_token("@roll", filters, result, "ctx")
        assert filters.roll is True

    def test_empty_token_not_consumed(self) -> None:
        filters, result = self._make()
        assert parse_filter_token("", filters, result, "ctx") is False

    def test_numeric_gt_sets_min(self) -> None:
        filters, result = self._make()
        parse_filter_token("oi>1000", filters, result, "ctx")
        assert filters.numeric_min.get("oi") == 1000.0

    def test_numeric_lt_sets_max(self) -> None:
        filters, result = self._make()
        parse_filter_token("price<500", filters, result, "ctx")
        assert filters.numeric_max.get("price") == 500.0

    def test_numeric_equals_sets_both_bounds(self) -> None:
        filters, result = self._make()
        parse_filter_token("oi=50", filters, result, "ctx")
        assert filters.numeric_min.get("oi") == 50.0
        assert filters.numeric_max.get("oi") == 50.0

    def test_range_value_sets_min_and_max(self) -> None:
        filters, result = self._make()
        parse_filter_token("price=100..200", filters, result, "ctx")
        assert filters.numeric_min.get("price") == 100.0
        assert filters.numeric_max.get("price") == 200.0

    def test_bool_filter_true(self) -> None:
        filters, result = self._make()
        parse_filter_token("tradable=true", filters, result, "ctx")
        assert filters.bools.get("tradable") is True

    def test_bool_filter_false(self) -> None:
        filters, result = self._make()
        parse_filter_token("tradable=0", filters, result, "ctx")
        assert filters.bools.get("tradable") is False

    def test_bool_filter_invalid_adds_warning(self) -> None:
        filters, result = self._make()
        parse_filter_token("tradable=maybe", filters, result, "ctx")
        assert len(result.warnings) == 1
        assert "Invalid boolean" in result.warnings[0]

    def test_month_filter_merges_months(self) -> None:
        filters, result = self._make()
        parse_filter_token("month=front|near", filters, result, "ctx")
        assert filters.months == ["front", "near"]

    def test_exclude_filter_adds_flags(self) -> None:
        filters, result = self._make()
        parse_filter_token("exclude=halted,suspended", filters, result, "ctx")
        assert "halted" in filters.exclude_flags
        assert "suspended" in filters.exclude_flags

    def test_top_n_filter(self) -> None:
        filters, result = self._make()
        parse_filter_token("oi=top5", filters, result, "ctx")
        assert filters.top_n.get("oi") == 5

    def test_exclude_dte_lte(self) -> None:
        filters, result = self._make()
        parse_filter_token("exclude_dte<=3", filters, result, "ctx")
        assert filters.exclude_dte_max == 3

    def test_exclude_dte_wrong_op_adds_warning(self) -> None:
        filters, result = self._make()
        parse_filter_token("exclude_dte>=3", filters, result, "ctx")
        assert any("exclude_dte" in w for w in result.warnings)

    def test_dte_with_roll_sets_roll_dte_max(self) -> None:
        filters, result = self._make()
        filters.roll = True
        parse_filter_token("dte<=5", filters, result, "ctx")
        assert filters.roll_dte_max == 5

    def test_dte_roll_wrong_op_adds_warning(self) -> None:
        filters, result = self._make()
        filters.roll = True
        parse_filter_token("dte>=5", filters, result, "ctx")
        assert any("roll dte" in w for w in result.warnings)

    def test_unknown_key_returns_false(self) -> None:
        filters, result = self._make()
        assert parse_filter_token("foobar=1", filters, result, "ctx") is False

    def test_no_operator_returns_false(self) -> None:
        filters, result = self._make()
        assert parse_filter_token("justtext", filters, result, "ctx") is False

    def test_invalid_numeric_adds_warning(self) -> None:
        filters, result = self._make()
        parse_filter_token("oi=notanumber", filters, result, "ctx")
        assert any("Invalid numeric" in w for w in result.warnings)

    def test_sector_enum_filter(self) -> None:
        filters, result = self._make()
        parse_filter_token("sector=tech|finance", filters, result, "ctx")
        assert "tech" in filters.enums.get("sector", set())
        assert "finance" in filters.enums.get("sector", set())

    def test_numeric_bounds_tighten_on_repeated_min(self) -> None:
        filters, result = self._make()
        parse_filter_token("oi>100", filters, result, "ctx")
        parse_filter_token("oi>200", filters, result, "ctx")
        # max of 100 and 200 = 200
        assert filters.numeric_min["oi"] == 200.0

    def test_numeric_bounds_tighten_on_repeated_max(self) -> None:
        filters, result = self._make()
        parse_filter_token("oi<500", filters, result, "ctx")
        parse_filter_token("oi<300", filters, result, "ctx")
        # min of 500 and 300 = 300
        assert filters.numeric_max["oi"] == 300.0


# ---------------------------------------------------------------------------
# parse_kv_tokens
# ---------------------------------------------------------------------------


class TestParseKvTokens:
    def test_exchange_key(self) -> None:
        result = parse_kv_tokens(["exchange=TSE"])
        assert result["exchange"] == "TSE"

    def test_exch_alias(self) -> None:
        result = parse_kv_tokens(["exch=OTC"])
        assert result["exchange"] == "OTC"

    def test_product_type_aliases(self) -> None:
        assert parse_kv_tokens(["product_type=STK"])["product_type"] == "STK"
        assert parse_kv_tokens(["security_type=FUT"])["product_type"] == "FUT"
        assert parse_kv_tokens(["type=OPT"])["product_type"] == "OPT"

    def test_tick_size_float(self) -> None:
        result = parse_kv_tokens(["tick=0.5"])
        assert result["tick_size"] == pytest.approx(0.5)

    def test_tick_size_invalid_goes_to_invalid(self) -> None:
        result = parse_kv_tokens(["tick=abc"])
        assert "_invalid" in result
        assert any("tick_size" in v for v in result["_invalid"])

    def test_price_scale_int(self) -> None:
        result = parse_kv_tokens(["scale=10000"])
        assert result["price_scale"] == 10000

    def test_price_scale_invalid(self) -> None:
        result = parse_kv_tokens(["price_scale=xyz"])
        assert "_invalid" in result

    def test_order_cond_aliases(self) -> None:
        assert parse_kv_tokens(["order_cond=ROD"])["order_cond"] == "ROD"
        assert parse_kv_tokens(["order_condition=IOC"])["order_cond"] == "IOC"

    def test_order_lot_aliases(self) -> None:
        assert parse_kv_tokens(["order_lot=Common"])["order_lot"] == "Common"
        assert parse_kv_tokens(["lot=Odd"])["order_lot"] == "Odd"

    def test_oc_type_aliases(self) -> None:
        assert parse_kv_tokens(["oc_type=Auto"])["oc_type"] == "Auto"
        assert parse_kv_tokens(["octype=New"])["oc_type"] == "New"

    def test_account(self) -> None:
        result = parse_kv_tokens(["account=ACC001"])
        assert result["account"] == "ACC001"

    def test_tags(self) -> None:
        result = parse_kv_tokens(["tags=alpha|beta"])
        assert result["tags"] == ["alpha", "beta"]

    def test_name_aliases(self) -> None:
        assert parse_kv_tokens(["name=TSMC"])["name"] == "TSMC"
        assert parse_kv_tokens(["contract_name=TXF"])["name"] == "TXF"

    def test_contract_size_float(self) -> None:
        result = parse_kv_tokens(["contract_size=50"])
        assert result["contract_size"] == pytest.approx(50.0)

    def test_contract_size_invalid(self) -> None:
        result = parse_kv_tokens(["size=bad"])
        assert "_invalid" in result

    def test_token_without_equals_skipped(self) -> None:
        result = parse_kv_tokens(["no_equals_here"])
        assert result == {}

    def test_empty_value_skipped(self) -> None:
        result = parse_kv_tokens(["exchange="])
        assert "exchange" not in result

    def test_multiple_tokens(self) -> None:
        result = parse_kv_tokens(["exchange=TSE", "tick=1.0", "scale=10000"])
        assert result["exchange"] == "TSE"
        assert result["tick_size"] == 1.0
        assert result["price_scale"] == 10000


# ---------------------------------------------------------------------------
# parse_csv_spec
# ---------------------------------------------------------------------------


class TestParseCsvSpec:
    def test_code_only(self) -> None:
        code, attrs = parse_csv_spec("2330")
        assert code == "2330"
        assert attrs == {}

    def test_code_and_exchange(self) -> None:
        code, attrs = parse_csv_spec("2330,TSE")
        assert code == "2330"
        assert attrs["exchange"] == "TSE"

    def test_code_exchange_tick(self) -> None:
        code, attrs = parse_csv_spec("2330,TSE,0.5")
        assert attrs["tick_size"] == pytest.approx(0.5)

    def test_code_exchange_tick_scale(self) -> None:
        code, attrs = parse_csv_spec("2330,TSE,0.5,10000")
        assert attrs["price_scale"] == 10000

    def test_code_exchange_tick_scale_tags(self) -> None:
        code, attrs = parse_csv_spec("2330,TSE,0.5,10000,alpha|beta")
        assert attrs["tags"] == ["alpha", "beta"]

    def test_invalid_tick_goes_to_invalid(self) -> None:
        _code, attrs = parse_csv_spec("SYM,FUT,notanum")
        assert "_invalid" in attrs

    def test_invalid_scale_goes_to_invalid(self) -> None:
        _code, attrs = parse_csv_spec("SYM,FUT,1.0,notint")
        assert "_invalid" in attrs

    def test_empty_spec_returns_empty(self) -> None:
        code, attrs = parse_csv_spec("")
        assert code == ""
        assert attrs == {}

    def test_whitespace_stripped(self) -> None:
        code, attrs = parse_csv_spec(" TXF , FUT ")
        assert code == "TXF"
        assert attrs["exchange"] == "FUT"


# ---------------------------------------------------------------------------
# parse_attrs_and_filters
# ---------------------------------------------------------------------------


class TestParseAttrsAndFilters:
    def _make_result(self) -> SymbolBuildResult:
        return SymbolBuildResult()

    def test_kv_token_goes_to_attrs(self) -> None:
        result = self._make_result()
        attrs, filters = parse_attrs_and_filters(["exchange=TSE"], result, "ctx")
        assert attrs["exchange"] == "TSE"

    def test_filter_token_goes_to_filters(self) -> None:
        result = self._make_result()
        _attrs, filters = parse_attrs_and_filters(["oi>1000"], result, "ctx")
        assert filters.numeric_min.get("oi") == 1000.0

    def test_unknown_token_adds_warning(self) -> None:
        result = self._make_result()
        parse_attrs_and_filters(["unknowntoken"], result, "ctx")
        assert any("Unknown token" in w for w in result.warnings)

    def test_roll_filter_parsed(self) -> None:
        result = self._make_result()
        _attrs, filters = parse_attrs_and_filters(["roll", "dte<=5"], result, "ctx")
        assert filters.roll is True
        assert filters.roll_dte_max == 5

    def test_mixed_tokens(self) -> None:
        result = self._make_result()
        attrs, filters = parse_attrs_and_filters(
            ["exchange=TSE", "oi>500", "tradable=true"],
            result,
            "ctx",
        )
        assert attrs["exchange"] == "TSE"
        assert filters.numeric_min["oi"] == 500.0
        assert filters.bools["tradable"] is True

    def test_empty_tokens_list(self) -> None:
        result = self._make_result()
        attrs, filters = parse_attrs_and_filters([], result, "ctx")
        assert attrs == {}
        assert result.errors == []
        assert result.warnings == []
