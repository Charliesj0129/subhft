"""Unit tests for hft_platform.config._symbols_types.

Covers: SymbolBuildResult, FilterSpec, derive_root, parse_date_key,
expiry_key, contract_dte_days, ContractIndex.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from hft_platform.config._symbols_types import (
    ContractIndex,
    FilterSpec,
    SymbolBuildResult,
    contract_dte_days,
    derive_root,
    expiry_key,
    parse_date_key,
)


# ---------------------------------------------------------------------------
# SymbolBuildResult
# ---------------------------------------------------------------------------


def test_symbol_build_result_ok_no_errors():
    result = SymbolBuildResult()
    assert result.ok() is True


def test_symbol_build_result_ok_with_errors():
    result = SymbolBuildResult(errors=["something went wrong"])
    assert result.ok() is False


def test_symbol_build_result_warnings_do_not_affect_ok():
    result = SymbolBuildResult(warnings=["minor warning"])
    assert result.ok() is True


def test_symbol_build_result_default_fields():
    result = SymbolBuildResult()
    assert result.symbols == []
    assert result.errors == []
    assert result.warnings == []


def test_symbol_build_result_with_symbols():
    result = SymbolBuildResult(symbols=[{"code": "2330"}])
    assert len(result.symbols) == 1
    assert result.ok() is True


# ---------------------------------------------------------------------------
# FilterSpec
# ---------------------------------------------------------------------------


def test_filter_spec_defaults():
    spec = FilterSpec()
    assert spec.bools == {}
    assert spec.enums == {}
    assert spec.numeric_min == {}
    assert spec.numeric_max == {}
    assert spec.top_n == {}
    assert spec.exclude_flags == set()
    assert spec.months is None
    assert spec.roll is False
    assert spec.roll_dte_max is None
    assert spec.exclude_dte_max is None


def test_filter_spec_merge_months_empty_list_noop():
    spec = FilterSpec()
    spec.merge_months([])
    assert spec.months is None


def test_filter_spec_merge_months_initial_none_sets_months():
    spec = FilterSpec()
    spec.merge_months(["202406", "202409"])
    assert spec.months == ["202406", "202409"]


def test_filter_spec_merge_months_second_call_deduplicates():
    spec = FilterSpec()
    spec.merge_months(["202406", "202409"])
    spec.merge_months(["202409", "202412"])  # 202409 duplicate
    assert spec.months is not None
    assert spec.months.count("202409") == 1
    assert "202412" in spec.months


def test_filter_spec_merge_months_preserves_order():
    spec = FilterSpec()
    spec.merge_months(["202412", "202409"])
    spec.merge_months(["202406"])
    assert spec.months[0] == "202412"
    assert "202406" in spec.months


# ---------------------------------------------------------------------------
# derive_root
# ---------------------------------------------------------------------------


def test_derive_root_normal_futures_code():
    assert derive_root("TXFD6") == "TXFD"


def test_derive_root_tmfd():
    assert derive_root("TMFD6") == "TMFD"


def test_derive_root_pure_alpha():
    assert derive_root("ABC") == "ABC"


def test_derive_root_numeric_only_returns_original():
    assert derive_root("12345") == "12345"


def test_derive_root_empty_string():
    assert derive_root("") == ""


def test_derive_root_mixed_prefix():
    assert derive_root("XY123") == "XY"


# ---------------------------------------------------------------------------
# parse_date_key
# ---------------------------------------------------------------------------


def test_parse_date_key_none():
    assert parse_date_key(None) is None


def test_parse_date_key_eight_digit_int():
    assert parse_date_key(20260329) == 20260329


def test_parse_date_key_eight_digit_float():
    assert parse_date_key(20260329.0) == 20260329


def test_parse_date_key_string_with_dashes():
    assert parse_date_key("2026-03-29") == 20260329


def test_parse_date_key_string_with_slashes():
    assert parse_date_key("2026/03/29") == 20260329


def test_parse_date_key_six_digit_string():
    result = parse_date_key("202603")
    assert result == 20260300


def test_parse_date_key_six_digit_int():
    result = parse_date_key(202603)
    assert result == 20260300


def test_parse_date_key_short_string_returns_none():
    assert parse_date_key("123") is None


def test_parse_date_key_short_int_returns_none():
    assert parse_date_key(123) is None


def test_parse_date_key_empty_string():
    assert parse_date_key("") is None


# ---------------------------------------------------------------------------
# expiry_key
# ---------------------------------------------------------------------------


def test_expiry_key_from_delivery_date():
    contract = {"code": "TXFD6", "delivery_date": 20260619}
    assert expiry_key(contract) == 20260619


def test_expiry_key_from_expiry_field():
    contract = {"code": "ABC", "expiry": "2026-12-18"}
    assert expiry_key(contract) == 20261218


def test_expiry_key_from_code_six_digits():
    contract = {"code": "TXFD202406"}
    assert expiry_key(contract) == 20240600


def test_expiry_key_no_date_no_match_returns_sentinel():
    contract = {"code": "NOMATCHING"}
    assert expiry_key(contract) == 99999999


def test_expiry_key_empty_contract():
    contract = {}
    assert expiry_key(contract) == 99999999


def test_expiry_key_due_date_field():
    contract = {"due_date": "20261218"}
    assert expiry_key(contract) == 20261218


# ---------------------------------------------------------------------------
# contract_dte_days
# ---------------------------------------------------------------------------


def test_contract_dte_days_valid_future_date():
    future_date = (datetime.utcnow() + timedelta(days=30)).strftime("%Y%m%d")
    contract = {"delivery_date": int(future_date)}
    result = contract_dte_days(contract)
    assert result is not None
    assert 25 <= result <= 35  # roughly 30 days ahead


def test_contract_dte_days_past_date_negative():
    past_date = (datetime.utcnow() - timedelta(days=10)).strftime("%Y%m%d")
    contract = {"delivery_date": int(past_date)}
    result = contract_dte_days(contract)
    assert result is not None
    assert result < 0


def test_contract_dte_days_no_date_fields_returns_none():
    contract = {"code": "NOMATCHING"}
    result = contract_dte_days(contract)
    assert result is None


def test_contract_dte_days_unparseable_date_skips_to_next():
    # An unparseable date string should trigger ValueError continue, then try next key.
    contract = {"delivery_date": "00000000", "expiry": "20260619"}
    result = contract_dte_days(contract)
    # expiry is a valid future date — should return something
    assert result is not None


def test_contract_dte_days_all_unparseable_returns_none():
    contract = {"delivery_date": "00000000", "expiry": "bad_date"}
    result = contract_dte_days(contract)
    assert result is None


# ---------------------------------------------------------------------------
# ContractIndex
# ---------------------------------------------------------------------------


def test_contract_index_empty_contracts():
    idx = ContractIndex(contracts=[])
    assert idx.by_code == {}
    assert idx.futures_by_root == {}
    assert idx.options_by_root == {}


def test_contract_index_by_code_populated():
    contracts = [{"code": "2330"}, {"code": "TXFD6"}]
    idx = ContractIndex(contracts=contracts)
    assert "2330" in idx.by_code
    assert "TXFD6" in idx.by_code


def test_contract_index_blank_code_skipped():
    contracts = [{"code": ""}, {"code": None}, {"code": "2330"}]
    idx = ContractIndex(contracts=contracts)
    assert "" not in idx.by_code
    assert None not in idx.by_code
    assert "2330" in idx.by_code


def test_contract_index_futures_classified_by_root():
    contracts = [
        {"code": "TXFD6", "root": "TXF", "type": "future"},
        {"code": "TXFG6", "root": "TXF", "type": "futures"},
    ]
    idx = ContractIndex(contracts=contracts)
    assert "TXF" in idx.futures_by_root
    assert len(idx.futures_by_root["TXF"]) == 2


def test_contract_index_options_classified_by_root():
    contracts = [
        {"code": "TXO20000C", "root": "TXO", "type": "option"},
        {"code": "TXO19000P", "root": "TXO", "type": "options"},
    ]
    idx = ContractIndex(contracts=contracts)
    assert "TXO" in idx.options_by_root
    assert len(idx.options_by_root["TXO"]) == 2


def test_contract_index_root_derived_from_code_when_missing():
    contracts = [{"code": "TXFD6", "type": "fut"}]
    idx = ContractIndex(contracts=contracts)
    # root derived via derive_root("TXFD6") = "TXFD"
    assert "TXFD" in idx.futures_by_root


def test_contract_index_metrics_dict_payload_normalized():
    metrics = {"2330": {"price": 850}}
    idx = ContractIndex(contracts=[], metrics_by_code=metrics)
    assert idx.metrics_by_code["2330"] == {"price": 850}


def test_contract_index_metrics_non_dict_payload_wrapped():
    metrics = {"2330": 850}  # non-dict value
    idx = ContractIndex(contracts=[], metrics_by_code=metrics)
    assert idx.metrics_by_code["2330"] == {"value": 850}


def test_contract_index_metrics_blank_code_skipped():
    metrics = {"": {"price": 100}, "  ": {"price": 200}, "2330": {"price": 300}}
    idx = ContractIndex(contracts=[], metrics_by_code=metrics)
    assert "" not in idx.metrics_by_code
    assert "  " not in idx.metrics_by_code
    assert "2330" in idx.metrics_by_code


def test_contract_index_mixed_contract_types():
    contracts = [
        {"code": "TXFD6", "root": "TXF", "type": "future"},
        {"code": "TXO20000C", "root": "TXO", "type": "option"},
        {"code": "2330"},  # no type — neither futures nor options
    ]
    idx = ContractIndex(contracts=contracts)
    assert "TXF" in idx.futures_by_root
    assert "TXO" in idx.options_by_root
    assert "2330" in idx.by_code
    # 2330 should not appear in futures or options by root
    assert "2330" not in idx.futures_by_root
    assert "2330" not in idx.options_by_root


def test_contract_index_security_type_field_fallback():
    # When "type" is absent, use "security_type".
    contracts = [{"code": "TXFD6", "root": "TXF", "security_type": "future"}]
    idx = ContractIndex(contracts=contracts)
    assert "TXF" in idx.futures_by_root
