"""Unit tests for hft_platform.config._symbols_filters."""

from __future__ import annotations

from typing import Any

import pytest

from hft_platform.config._symbols_filters import (
    _coerce_numeric,
    _has_exclude_flag,
    apply_filters,
    filters_active,
    resolve_metric,
)
from hft_platform.config._symbols_types import (
    ContractIndex,
    FilterSpec,
    SymbolBuildResult,
)

# ---------------------------------------------------------------------------
# _coerce_numeric
# ---------------------------------------------------------------------------


class TestCoerceNumeric:
    def test_none_returns_none(self) -> None:
        assert _coerce_numeric(None) is None

    def test_int_value(self) -> None:
        assert _coerce_numeric(42) == pytest.approx(42.0)

    def test_float_value(self) -> None:
        assert _coerce_numeric(3.14) == pytest.approx(3.14)

    def test_numeric_string(self) -> None:
        assert _coerce_numeric("100") == pytest.approx(100.0)

    def test_invalid_string_returns_none(self) -> None:
        assert _coerce_numeric("abc") is None


# ---------------------------------------------------------------------------
# resolve_metric
# ---------------------------------------------------------------------------


class TestResolveMetric:
    def _contract(self, **kwargs: Any) -> dict[str, Any]:
        return {"code": "SYM", **kwargs}

    def test_dte_from_far_future_delivery(self) -> None:
        contract = self._contract(delivery_date="20990101")
        result = resolve_metric(contract, {}, "dte")
        # Far future — DTE should be positive and large
        assert result is not None
        assert result > 10000

    def test_dte_missing_expiry_returns_none(self) -> None:
        contract = self._contract()
        result = resolve_metric(contract, {}, "dte")
        assert result is None

    def test_moneyness_computed(self) -> None:
        contract = self._contract(strike=100)
        result = resolve_metric(contract, {}, "moneyness", reference=200.0)
        assert result == pytest.approx(0.5)

    def test_moneyness_from_metrics(self) -> None:
        contract = self._contract(strike=150)
        metrics = {"SYM": {"underlying_price": 300.0}}
        result = resolve_metric(contract, metrics, "moneyness")
        assert result == pytest.approx(0.5)

    def test_moneyness_no_strike_returns_none(self) -> None:
        contract = self._contract()
        result = resolve_metric(contract, {}, "moneyness", reference=100.0)
        assert result is None

    def test_moneyness_no_reference_returns_none(self) -> None:
        contract = self._contract(strike=100)
        result = resolve_metric(contract, {}, "moneyness")
        assert result is None

    def test_moneyness_zero_reference_returns_none(self) -> None:
        contract = self._contract(strike=100)
        result = resolve_metric(contract, {}, "moneyness", reference=0.0)
        assert result is None

    def test_metric_from_metrics_dict(self) -> None:
        contract = self._contract()
        metrics = {"SYM": {"oi": 5000}}
        result = resolve_metric(contract, metrics, "oi")
        assert result == pytest.approx(5000.0)

    def test_metric_fallback_to_contract_field(self) -> None:
        contract = self._contract(price=300)
        result = resolve_metric(contract, {}, "price")
        assert result == pytest.approx(300.0)

    def test_metric_alias_resolution(self) -> None:
        contract = self._contract()
        # "open_interest" is an alias for "oi"
        metrics = {"SYM": {"open_interest": 7500}}
        result = resolve_metric(contract, metrics, "oi")
        assert result == pytest.approx(7500.0)

    def test_bool_metric_true(self) -> None:
        contract = self._contract()
        metrics = {"SYM": {"tradable": True}}
        result = resolve_metric(contract, metrics, "tradable")
        assert result is True

    def test_bool_metric_string_conversion(self) -> None:
        contract = self._contract()
        metrics = {"SYM": {"tradable": "yes"}}
        result = resolve_metric(contract, metrics, "tradable")
        assert result is True

    def test_bool_metric_missing_returns_none(self) -> None:
        contract = self._contract()
        result = resolve_metric(contract, {}, "tradable")
        assert result is None

    def test_list_metric_as_string(self) -> None:
        contract = self._contract()
        metrics = {"SYM": {"sector": "tech"}}
        result = resolve_metric(contract, metrics, "sector")
        assert isinstance(result, set)
        assert "tech" in result

    def test_list_metric_as_list(self) -> None:
        contract = self._contract()
        metrics = {"SYM": {"sector": ["tech", "finance"]}}
        result = resolve_metric(contract, metrics, "sector")
        assert isinstance(result, set)
        assert "tech" in result
        assert "finance" in result

    def test_missing_metrics_dict_uses_contract(self) -> None:
        contract = self._contract(oi=1000)
        result = resolve_metric(contract, {}, "oi")
        assert result == pytest.approx(1000.0)

    def test_moneyness_strike_price_alias(self) -> None:
        contract = self._contract(strike_price=200)
        result = resolve_metric(contract, {}, "moneyness", reference=400.0)
        assert result == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# _has_exclude_flag
# ---------------------------------------------------------------------------


class TestHasExcludeFlag:
    def test_empty_metrics_returns_false(self) -> None:
        assert _has_exclude_flag({}, {"halted"}) is False

    def test_direct_key_match(self) -> None:
        metrics = {"halted": True}
        assert _has_exclude_flag(metrics, {"halted"}) is True

    def test_direct_key_falsy_does_not_match(self) -> None:
        metrics = {"halted": False}
        assert _has_exclude_flag(metrics, {"halted"}) is False

    def test_flags_list_match(self) -> None:
        metrics = {"flags": ["halted", "restricted"]}
        assert _has_exclude_flag(metrics, {"halted"}) is True

    def test_flags_tuple_match(self) -> None:
        metrics = {"flags": ("suspended",)}
        assert _has_exclude_flag(metrics, {"suspended"}) is True

    def test_flags_set_match(self) -> None:
        metrics = {"flags": {"delisted"}}
        assert _has_exclude_flag(metrics, {"delisted"}) is True

    def test_flags_string_pipe_delimited_match(self) -> None:
        metrics = {"flags": "halted|suspended"}
        assert _has_exclude_flag(metrics, {"halted"}) is True

    def test_flags_string_comma_delimited_match(self) -> None:
        metrics = {"flags": "halted,suspended"}
        assert _has_exclude_flag(metrics, {"suspended"}) is True

    def test_is_prefix_match(self) -> None:
        metrics = {"is_halted": True}
        assert _has_exclude_flag(metrics, {"halted"}) is True

    def test_is_prefix_falsy_does_not_match(self) -> None:
        metrics = {"is_halted": False}
        assert _has_exclude_flag(metrics, {"halted"}) is False

    def test_flag_not_present_returns_false(self) -> None:
        metrics = {"flags": ["active"]}
        assert _has_exclude_flag(metrics, {"halted"}) is False

    def test_multiple_flags_any_match(self) -> None:
        metrics = {"flags": ["suspended"]}
        assert _has_exclude_flag(metrics, {"halted", "suspended"}) is True

    def test_no_flags_in_metrics_returns_false(self) -> None:
        metrics = {"price": 100}
        assert _has_exclude_flag(metrics, {"halted"}) is False


# ---------------------------------------------------------------------------
# filters_active
# ---------------------------------------------------------------------------


class TestFiltersActive:
    def test_empty_filters_inactive(self) -> None:
        assert filters_active(FilterSpec()) is False

    def test_bool_filter_active(self) -> None:
        f = FilterSpec(bools={"tradable": True})
        assert filters_active(f) is True

    def test_enum_filter_active(self) -> None:
        f = FilterSpec(enums={"sector": {"tech"}})
        assert filters_active(f) is True

    def test_numeric_min_active(self) -> None:
        f = FilterSpec(numeric_min={"oi": 100.0})
        assert filters_active(f) is True

    def test_numeric_max_active(self) -> None:
        f = FilterSpec(numeric_max={"price": 500.0})
        assert filters_active(f) is True

    def test_top_n_active(self) -> None:
        f = FilterSpec(top_n={"oi": 5})
        assert filters_active(f) is True

    def test_exclude_flags_active(self) -> None:
        f = FilterSpec(exclude_flags={"halted"})
        assert filters_active(f) is True

    def test_months_active(self) -> None:
        f = FilterSpec(months=["front"])
        assert filters_active(f) is True

    def test_roll_active(self) -> None:
        f = FilterSpec(roll=True)
        assert filters_active(f) is True

    def test_roll_dte_max_active(self) -> None:
        f = FilterSpec(roll_dte_max=5)
        assert filters_active(f) is True

    def test_exclude_dte_max_active(self) -> None:
        f = FilterSpec(exclude_dte_max=3)
        assert filters_active(f) is True


# ---------------------------------------------------------------------------
# apply_filters
# ---------------------------------------------------------------------------


def _make_result() -> SymbolBuildResult:
    return SymbolBuildResult()


def _contracts(*codes: str) -> list[dict[str, Any]]:
    return [{"code": c} for c in codes]


class TestApplyFiltersNoOp:
    def test_empty_contracts_returns_empty(self) -> None:
        f = FilterSpec(numeric_min={"oi": 100.0})
        result = _make_result()
        assert apply_filters([], f, result, None, "ctx") == []

    def test_inactive_filters_returns_all(self) -> None:
        contracts = _contracts("A", "B", "C")
        result = _make_result()
        out = apply_filters(contracts, FilterSpec(), result, None, "ctx")
        assert len(out) == 3


class TestApplyFiltersBool:
    def test_bool_filter_keeps_matching(self) -> None:
        contracts = [
            {"code": "A"},
            {"code": "B"},
        ]
        metrics = {"A": {"tradable": True}, "B": {"tradable": False}}
        index = ContractIndex(contracts=contracts, metrics_by_code=metrics)
        f = FilterSpec(bools={"tradable": True})
        result = _make_result()
        out = apply_filters(contracts, f, result, index, "ctx")
        assert [c["code"] for c in out] == ["A"]

    def test_bool_filter_missing_metric_excludes(self) -> None:
        contracts = [{"code": "A"}, {"code": "B"}]
        metrics: dict[str, Any] = {"A": {"tradable": True}}  # B has no tradable
        index = ContractIndex(contracts=contracts, metrics_by_code=metrics)
        f = FilterSpec(bools={"tradable": True})
        result = _make_result()
        out = apply_filters(contracts, f, result, index, "ctx")
        assert [c["code"] for c in out] == ["A"]


class TestApplyFiltersEnum:
    def test_enum_filter_keeps_matching_sector(self) -> None:
        contracts = [{"code": "A"}, {"code": "B"}, {"code": "C"}]
        metrics = {
            "A": {"sector": "tech"},
            "B": {"sector": "finance"},
            "C": {"sector": "tech"},
        }
        index = ContractIndex(contracts=contracts, metrics_by_code=metrics)
        f = FilterSpec(enums={"sector": {"tech"}})
        result = _make_result()
        out = apply_filters(contracts, f, result, index, "ctx")
        codes = [c["code"] for c in out]
        assert sorted(codes) == ["A", "C"]

    def test_enum_filter_missing_metric_excludes(self) -> None:
        contracts = [{"code": "A"}, {"code": "B"}]
        metrics: dict[str, Any] = {"A": {"sector": "tech"}}
        index = ContractIndex(contracts=contracts, metrics_by_code=metrics)
        f = FilterSpec(enums={"sector": {"tech"}})
        result = _make_result()
        out = apply_filters(contracts, f, result, index, "ctx")
        assert [c["code"] for c in out] == ["A"]


class TestApplyFiltersNumeric:
    def test_numeric_min_filter(self) -> None:
        contracts = [{"code": "A"}, {"code": "B"}, {"code": "C"}]
        metrics = {"A": {"oi": 500}, "B": {"oi": 1500}, "C": {"oi": 1000}}
        index = ContractIndex(contracts=contracts, metrics_by_code=metrics)
        f = FilterSpec(numeric_min={"oi": 1000.0})
        result = _make_result()
        out = apply_filters(contracts, f, result, index, "ctx")
        codes = sorted(c["code"] for c in out)
        assert codes == ["B", "C"]

    def test_numeric_max_filter(self) -> None:
        contracts = [{"code": "A"}, {"code": "B"}]
        metrics = {"A": {"price": 100}, "B": {"price": 300}}
        index = ContractIndex(contracts=contracts, metrics_by_code=metrics)
        f = FilterSpec(numeric_max={"price": 200.0})
        result = _make_result()
        out = apply_filters(contracts, f, result, index, "ctx")
        assert [c["code"] for c in out] == ["A"]

    def test_numeric_range_filter(self) -> None:
        contracts = [{"code": "A"}, {"code": "B"}, {"code": "C"}]
        metrics = {"A": {"price": 50}, "B": {"price": 150}, "C": {"price": 250}}
        index = ContractIndex(contracts=contracts, metrics_by_code=metrics)
        f = FilterSpec(numeric_min={"price": 100.0}, numeric_max={"price": 200.0})
        result = _make_result()
        out = apply_filters(contracts, f, result, index, "ctx")
        assert [c["code"] for c in out] == ["B"]

    def test_numeric_missing_metric_adds_error_when_all_missing(self) -> None:
        contracts = [{"code": "A"}, {"code": "B"}]
        # No metrics provided for oi
        index = ContractIndex(contracts=contracts, metrics_by_code={})
        f = FilterSpec(numeric_min={"oi": 100.0})
        result = _make_result()
        apply_filters(contracts, f, result, index, "ctx")
        assert any("oi" in e for e in result.errors)


class TestApplyFiltersTopN:
    def test_top_n_returns_n_highest(self) -> None:
        contracts = [{"code": "A"}, {"code": "B"}, {"code": "C"}, {"code": "D"}]
        metrics = {"A": {"oi": 100}, "B": {"oi": 400}, "C": {"oi": 200}, "D": {"oi": 300}}
        index = ContractIndex(contracts=contracts, metrics_by_code=metrics)
        f = FilterSpec(top_n={"oi": 2})
        result = _make_result()
        out = apply_filters(contracts, f, result, index, "ctx")
        codes = sorted(c["code"] for c in out)
        assert codes == ["B", "D"]

    def test_top_n_no_metrics_adds_error(self) -> None:
        contracts = [{"code": "A"}, {"code": "B"}]
        index = ContractIndex(contracts=contracts, metrics_by_code={})
        f = FilterSpec(top_n={"oi": 2})
        result = _make_result()
        out = apply_filters(contracts, f, result, index, "ctx")
        assert out == []
        assert any("top" in e for e in result.errors)


class TestApplyFiltersExcludeDte:
    def test_exclude_dte_filters_near_expiry(self) -> None:
        contracts = [
            {"code": "A", "delivery_date": "20990101"},  # far future
            {"code": "B", "delivery_date": "20260101"},  # past / near
        ]
        index = ContractIndex(contracts=contracts, metrics_by_code={})
        f = FilterSpec(exclude_dte_max=10000)  # exclude anything with DTE <= 10000
        result = _make_result()
        out = apply_filters(contracts, f, result, index, "ctx")
        # B has DTE < 10000 → excluded; A has very large DTE → kept
        codes = [c["code"] for c in out]
        assert "A" in codes
        assert "B" not in codes

    def test_exclude_dte_missing_expiry_excludes_contract(self) -> None:
        contracts = [{"code": "A"}]  # no delivery_date
        index = ContractIndex(contracts=contracts, metrics_by_code={})
        f = FilterSpec(exclude_dte_max=30)
        result = _make_result()
        out = apply_filters(contracts, f, result, index, "ctx")
        assert out == []


class TestApplyFiltersExcludeFlags:
    def test_exclude_flag_removes_flagged(self) -> None:
        contracts = [{"code": "A"}, {"code": "B"}]
        metrics = {"A": {"is_halted": True}, "B": {}}
        index = ContractIndex(contracts=contracts, metrics_by_code=metrics)
        f = FilterSpec(exclude_flags={"halted"})
        result = _make_result()
        out = apply_filters(contracts, f, result, index, "ctx")
        assert [c["code"] for c in out] == ["B"]

    def test_exclude_flag_not_present_keeps_contract(self) -> None:
        contracts = [{"code": "A"}]
        metrics = {"A": {"price": 100}}
        index = ContractIndex(contracts=contracts, metrics_by_code=metrics)
        f = FilterSpec(exclude_flags={"halted"})
        result = _make_result()
        out = apply_filters(contracts, f, result, index, "ctx")
        assert [c["code"] for c in out] == ["A"]


class TestApplyFiltersNoIndex:
    def test_no_contract_index_inactive_filters_passthrough(self) -> None:
        contracts = _contracts("X", "Y")
        result = _make_result()
        out = apply_filters(contracts, FilterSpec(), result, None, "ctx")
        assert len(out) == 2

    def test_no_contract_index_numeric_filter_uses_contract_fields(self) -> None:
        contracts = [{"code": "A", "price": 200}, {"code": "B", "price": 50}]
        f = FilterSpec(numeric_min={"price": 100.0})
        result = _make_result()
        out = apply_filters(contracts, f, result, None, "ctx")
        assert [c["code"] for c in out] == ["A"]
