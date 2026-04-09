"""Comprehensive unit tests for hft_platform.config._symbols_expansion.

Covers all public functions and their branches including:
- _default_exchange_for_code
- _normalize_option_right
- _parse_selector
- _pick_reference_price
- build_entry
- _group_by_expiry
- _expand_synthetic
- _expand_futures
- _expand_options
- expand_spec
"""

from __future__ import annotations

from hft_platform.config._symbols_expansion import (
    _default_exchange_for_code,
    _expand_futures,
    _expand_options,
    _expand_synthetic,
    _group_by_expiry,
    _normalize_option_right,
    _parse_selector,
    _pick_reference_price,
    build_entry,
    expand_spec,
)
from hft_platform.config._symbols_types import (
    PLUS_MINUS,
    ContractIndex,
    FilterSpec,
    SymbolBuildResult,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_result() -> SymbolBuildResult:
    return SymbolBuildResult()


def _make_futures_contract(
    code: str,
    root: str | None = None,
    delivery_date: int = 20270101,
    **extra: object,
) -> dict:
    """Create a minimal futures contract dict that ContractIndex will index."""
    c: dict = {
        "code": code,
        "type": "future",
        "delivery_date": delivery_date,
    }
    if root is not None:
        c["root"] = root
    c.update(extra)
    return c


def _make_option_contract(
    code: str,
    root: str | None = None,
    strike: float = 18000.0,
    right: str = "C",
    delivery_date: int = 20270101,
    **extra: object,
) -> dict:
    c: dict = {
        "code": code,
        "type": "option",
        "strike": strike,
        "right": right,
        "delivery_date": delivery_date,
    }
    if root is not None:
        c["root"] = root
    c.update(extra)
    return c


def _make_contract_index(contracts: list[dict]) -> ContractIndex:
    return ContractIndex(contracts=contracts)


# ---------------------------------------------------------------------------
# _default_exchange_for_code
# ---------------------------------------------------------------------------


class TestDefaultExchangeForCode:
    def test_digit_code_returns_tse(self):
        assert _default_exchange_for_code("2330") == "TSE"

    def test_alpha_code_returns_fut(self):
        assert _default_exchange_for_code("TX") == "FUT"

    def test_mixed_code_returns_fut(self):
        # starts with letter → not all digits
        assert _default_exchange_for_code("TXF123") == "FUT"

    def test_single_digit_returns_tse(self):
        assert _default_exchange_for_code("0") == "TSE"

    def test_empty_string_returns_fut(self):
        # empty string is not all digits
        assert _default_exchange_for_code("") == "FUT"


# ---------------------------------------------------------------------------
# _normalize_option_right
# ---------------------------------------------------------------------------


class TestNormalizeOptionRight:
    def test_call_string(self):
        assert _normalize_option_right("CALL") == "C"

    def test_call_lowercase(self):
        assert _normalize_option_right("call") == "C"

    def test_c_shorthand(self):
        assert _normalize_option_right("C") == "C"

    def test_put_string(self):
        assert _normalize_option_right("PUT") == "P"

    def test_put_lowercase(self):
        assert _normalize_option_right("put") == "P"

    def test_p_shorthand(self):
        assert _normalize_option_right("P") == "P"

    def test_unknown_returns_empty(self):
        assert _normalize_option_right("X") == ""

    def test_none_returns_empty(self):
        assert _normalize_option_right(None) == ""

    def test_empty_string_returns_empty(self):
        assert _normalize_option_right("") == ""

    def test_mixed_case_call(self):
        assert _normalize_option_right("Call") == "C"


# ---------------------------------------------------------------------------
# _parse_selector
# ---------------------------------------------------------------------------


class TestParseSelector:
    def test_atm_no_offset(self):
        mode, offset = _parse_selector("ATM")
        assert mode == "ATM"
        assert offset == 0

    def test_atm_plus_offset(self):
        mode, offset = _parse_selector("ATM+3")
        assert mode == "ATM"
        assert offset == 3

    def test_atm_minus_offset(self):
        mode, offset = _parse_selector("ATM-2")
        assert mode == "ATM"
        assert offset == 2

    def test_otm_no_offset(self):
        mode, offset = _parse_selector("OTM")
        assert mode == "OTM"
        assert offset == 0

    def test_otm_plus_offset(self):
        mode, offset = _parse_selector("OTM+2")
        assert mode == "OTM"
        assert offset == 2

    def test_atm_plus_minus_unicode(self):
        # Using the PLUS_MINUS unicode character
        mode, offset = _parse_selector(f"ATM{PLUS_MINUS}2")
        assert mode == "ATM"
        assert offset == 2

    def test_atm_plus_minus_literal(self):
        mode, offset = _parse_selector("ATM+/-2")
        assert mode == "ATM"
        assert offset == 2

    def test_unknown_returns_unknown(self):
        mode, offset = _parse_selector("ITM+1")
        assert mode == "UNKNOWN"
        assert offset == 0

    def test_lowercase_atm(self):
        mode, offset = _parse_selector("atm+1")
        assert mode == "ATM"
        assert offset == 1

    def test_lowercase_otm(self):
        mode, offset = _parse_selector("otm+3")
        assert mode == "OTM"
        assert offset == 3


# ---------------------------------------------------------------------------
# _pick_reference_price
# ---------------------------------------------------------------------------


class TestPickReferencePrice:
    def test_returns_reference_field(self):
        contracts = [{"reference": 18000.0}]
        assert _pick_reference_price(contracts) == 18000.0

    def test_returns_reference_price_field(self):
        contracts = [{"reference_price": 17500.0}]
        assert _pick_reference_price(contracts) == 17500.0

    def test_returns_underlying_price(self):
        contracts = [{"underlying_price": 17000.0}]
        assert _pick_reference_price(contracts) == 17000.0

    def test_returns_close_field(self):
        contracts = [{"close": 16000.0}]
        assert _pick_reference_price(contracts) == 16000.0

    def test_returns_none_when_no_data(self):
        contracts = [{"strike": 18000}]
        assert _pick_reference_price(contracts) is None

    def test_empty_contracts_returns_none(self):
        assert _pick_reference_price([]) is None

    def test_skips_non_numeric_values(self):
        contracts = [{"reference": "not_a_number"}, {"close": 15000.0}]
        assert _pick_reference_price(contracts) == 15000.0

    def test_priority_reference_over_close(self):
        contracts = [{"reference": 18000.0, "close": 17000.0}]
        assert _pick_reference_price(contracts) == 18000.0

    def test_string_numeric_is_coerced(self):
        contracts = [{"reference": "18500"}]
        assert _pick_reference_price(contracts) == 18500.0


# ---------------------------------------------------------------------------
# build_entry
# ---------------------------------------------------------------------------


class TestBuildEntry:
    def test_empty_code_returns_none(self):
        result = _make_result()
        entry = build_entry("", {}, None, result)
        assert entry is None

    def test_basic_entry_has_code(self):
        result = _make_result()
        entry = build_entry("2330", {}, None, result)
        assert entry is not None
        assert entry["code"] == "2330"

    def test_default_exchange_for_digit_code(self):
        result = _make_result()
        entry = build_entry("2330", {}, None, result)
        assert entry["exchange"] == "TSE"
        assert len(result.warnings) == 1
        assert "2330" in result.warnings[0]

    def test_default_exchange_for_alpha_code(self):
        result = _make_result()
        entry = build_entry("TX", {}, None, result)
        assert entry["exchange"] == "FUT"

    def test_contract_fields_copied(self):
        result = _make_result()
        contract = {
            "name": "台積電",
            "exchange": "TSE",
            "tick_size": 0.5,
            "price_scale": 10000,
            "contract_size": 1000,
        }
        entry = build_entry("2330", {}, contract, result)
        assert entry["name"] == "台積電"
        assert entry["exchange"] == "TSE"
        assert entry["tick_size"] == 0.5
        assert entry["price_scale"] == 10000
        assert entry["contract_size"] == 1000

    def test_contract_type_to_product_type(self):
        result = _make_result()
        contract = {"exchange": "FUT", "type": "future"}
        entry = build_entry("TX", {}, contract, result)
        assert entry["product_type"] == "future"

    def test_contract_security_type_used_when_no_type(self):
        result = _make_result()
        contract = {"exchange": "OPT", "security_type": "option"}
        entry = build_entry("TXO", {}, contract, result)
        assert entry["product_type"] == "option"

    def test_attrs_override_contract(self):
        result = _make_result()
        contract = {"exchange": "TSE", "name": "Old Name"}
        attrs = {"name": "New Name"}
        entry = build_entry("2330", attrs, contract, result)
        assert entry["name"] == "New Name"

    def test_none_attr_values_excluded(self):
        result = _make_result()
        attrs = {"name": None, "exchange": "TSE"}
        entry = build_entry("TX", attrs, None, result)
        assert "name" not in entry
        assert entry["exchange"] == "TSE"

    def test_extra_tags_merged(self):
        result = _make_result()
        entry = build_entry("TX", {}, None, result, extra_tags=["futures", "front_month"])
        assert "futures" in entry["tags"]
        assert "front_month" in entry["tags"]

    def test_existing_tags_merged_with_extra_tags(self):
        result = _make_result()
        attrs = {"tags": ["watchlist"]}
        entry = build_entry("TX", attrs, None, result, extra_tags=["futures"])
        assert "watchlist" in entry["tags"]
        assert "futures" in entry["tags"]

    def test_tags_deduplicated(self):
        result = _make_result()
        attrs = {"tags": ["futures"]}
        entry = build_entry("TX", attrs, None, result, extra_tags=["futures"])
        assert entry["tags"].count("futures") == 1

    def test_no_tags_field_when_empty(self):
        result = _make_result()
        entry = build_entry("TX", {}, None, result, extra_tags=[])
        # tags field only present if non-empty
        if "tags" in entry:
            assert entry["tags"] == []

    def test_contract_exchange_used_when_present(self):
        result = _make_result()
        contract = {"exchange": "TAIFEX"}
        entry = build_entry("TX", {}, contract, result)
        assert entry["exchange"] == "TAIFEX"
        # No warning when exchange is found
        assert not result.warnings


# ---------------------------------------------------------------------------
# _group_by_expiry
# ---------------------------------------------------------------------------


class TestGroupByExpiry:
    def test_groups_by_delivery_date(self):
        contracts = [
            {"code": "TXF1", "delivery_date": 20270301},
            {"code": "TXF2", "delivery_date": 20270401},
            {"code": "TXF3", "delivery_date": 20270301},
        ]
        groups = _group_by_expiry(contracts)
        assert len(groups) == 2
        # First group is earlier expiry
        codes_first = {c["code"] for c in groups[0]}
        assert "TXF1" in codes_first
        assert "TXF3" in codes_first

    def test_sorted_by_expiry_ascending(self):
        contracts = [
            {"code": "B", "delivery_date": 20270401},
            {"code": "A", "delivery_date": 20270301},
        ]
        groups = _group_by_expiry(contracts)
        assert groups[0][0]["code"] == "A"
        assert groups[1][0]["code"] == "B"

    def test_single_contract(self):
        contracts = [{"code": "TX", "delivery_date": 20270301}]
        groups = _group_by_expiry(contracts)
        assert len(groups) == 1
        assert groups[0][0]["code"] == "TX"

    def test_no_delivery_date_uses_fallback_key(self):
        contracts = [{"code": "TXF1"}]
        groups = _group_by_expiry(contracts)
        assert len(groups) == 1


# ---------------------------------------------------------------------------
# _expand_synthetic
# ---------------------------------------------------------------------------


class TestExpandSynthetic:
    def test_generates_correct_count(self):
        result = _make_result()
        _expand_synthetic("synth", 3, {}, result)
        assert len(result.symbols) == 3

    def test_code_format(self):
        result = _make_result()
        _expand_synthetic("synth", 2, {}, result)
        codes = [s["code"] for s in result.symbols]
        assert codes == ["SYNTH0001", "SYNTH0002"]

    def test_prefix_uppercased(self):
        result = _make_result()
        _expand_synthetic("stress", 1, {}, result)
        assert result.symbols[0]["code"] == "STRESS0001"

    def test_default_exchange_is_fut_for_alpha_code(self):
        # build_entry sees "SYNTH0001" (alpha), applies _default_exchange_for_code → "FUT";
        # setdefault("exchange", "SIM") does nothing since exchange is already set
        result = _make_result()
        _expand_synthetic("synth", 1, {}, result)
        assert result.symbols[0]["exchange"] == "FUT"

    def test_synthetic_and_stress_tags(self):
        result = _make_result()
        _expand_synthetic("synth", 1, {}, result)
        tags = result.symbols[0]["tags"]
        assert "synthetic" in tags
        assert "stress" in tags

    def test_zero_count_produces_error(self):
        result = _make_result()
        _expand_synthetic("synth", 0, {}, result)
        assert len(result.symbols) == 0
        assert len(result.errors) == 1
        assert "positive" in result.errors[0]

    def test_negative_count_produces_error(self):
        result = _make_result()
        _expand_synthetic("synth", -1, {}, result)
        assert len(result.symbols) == 0
        assert len(result.errors) == 1

    def test_attrs_included_in_entry(self):
        result = _make_result()
        attrs = {"account": "test_account"}
        _expand_synthetic("synth", 1, attrs, result)
        assert result.symbols[0]["account"] == "test_account"


# ---------------------------------------------------------------------------
# _expand_futures
# ---------------------------------------------------------------------------


class TestExpandFutures:
    def _make_index(self, contracts: list[dict] | None = None) -> ContractIndex:
        if contracts is None:
            contracts = [
                _make_futures_contract("TXFD7", root="TX", delivery_date=20270301),
                _make_futures_contract("TXFG7", root="TX", delivery_date=20270401),
                _make_futures_contract("TXFH7", root="TX", delivery_date=20270501),
            ]
        return _make_contract_index(contracts)

    def test_front_month_selected(self):
        result = _make_result()
        idx = self._make_index()
        _expand_futures("TX", "front", {}, idx, result)
        assert len(result.symbols) == 1
        assert result.symbols[0]["code"] == "TXFD7"

    def test_next_month_selected(self):
        result = _make_result()
        idx = self._make_index()
        _expand_futures("TX", "next", {}, idx, result)
        assert len(result.symbols) == 1
        assert result.symbols[0]["code"] == "TXFG7"

    def test_far_month_selected(self):
        result = _make_result()
        idx = self._make_index()
        _expand_futures("TX", "far", {}, idx, result)
        assert len(result.symbols) == 1
        assert result.symbols[0]["code"] == "TXFH7"

    def test_near_alias_for_front(self):
        result = _make_result()
        idx = self._make_index()
        _expand_futures("TX", "near", {}, idx, result)
        assert len(result.symbols) == 1
        assert result.symbols[0]["code"] == "TXFD7"

    def test_no_contract_index_produces_error(self):
        result = _make_result()
        _expand_futures("TX", "front", {}, None, result)
        assert len(result.errors) == 1
        assert "contract cache" in result.errors[0]

    def test_unknown_root_produces_error(self):
        result = _make_result()
        idx = self._make_index()
        _expand_futures("MISSING", "front", {}, idx, result)
        assert len(result.errors) == 1
        assert "MISSING" in result.errors[0]

    def test_unknown_month_token_produces_error(self):
        result = _make_result()
        idx = self._make_index()
        _expand_futures("TX", "middle", {}, idx, result)
        assert len(result.errors) >= 1

    def test_out_of_range_month_produces_error(self):
        # Only 2 contracts = only front and next (index 0, 1). "far" (idx=2) is out of range.
        contracts = [
            _make_futures_contract("TXFD7", root="TX", delivery_date=20270301),
            _make_futures_contract("TXFG7", root="TX", delivery_date=20270401),
        ]
        result = _make_result()
        idx = _make_contract_index(contracts)
        _expand_futures("TX", "far", {}, idx, result)
        assert len(result.errors) >= 1

    def test_r1_r2_contracts_filtered(self):
        contracts = [
            _make_futures_contract("TXFD7R1", root="TX", delivery_date=20270301),
            _make_futures_contract("TXFD7R2", root="TX", delivery_date=20270301),
            _make_futures_contract("TXFD7", root="TX", delivery_date=20270301),
        ]
        result = _make_result()
        idx = _make_contract_index(contracts)
        _expand_futures("TX", "front", {}, idx, result)
        assert len(result.symbols) == 1
        assert result.symbols[0]["code"] == "TXFD7"

    def test_all_r1_r2_produces_error(self):
        contracts = [
            _make_futures_contract("TXFD7R1", root="TX", delivery_date=20270301),
            _make_futures_contract("TXFD7R2", root="TX", delivery_date=20270301),
        ]
        result = _make_result()
        idx = _make_contract_index(contracts)
        _expand_futures("TX", "front", {}, idx, result)
        assert len(result.errors) >= 1

    def test_futures_tag_applied(self):
        result = _make_result()
        idx = self._make_index()
        _expand_futures("TX", "front", {}, idx, result)
        tags = result.symbols[0]["tags"]
        assert "futures" in tags

    def test_front_month_tag_applied(self):
        result = _make_result()
        idx = self._make_index()
        _expand_futures("TX", "front", {}, idx, result)
        tags = result.symbols[0]["tags"]
        assert "front_month" in tags

    def test_default_exchange_is_fut(self):
        result = _make_result()
        idx = self._make_index()
        _expand_futures("TX", "front", {}, idx, result)
        assert result.symbols[0]["exchange"] == "FUT"

    def test_roll_mode_stays_on_front_when_dte_high(self):
        # delivery_date far in future → DTE > 5 → stay on front
        result = _make_result()
        idx = self._make_index()
        filters = FilterSpec(roll=True)
        _expand_futures("TX", "roll", {}, idx, result, filters)
        assert len(result.symbols) == 1
        assert result.symbols[0]["code"] == "TXFD7"

    def test_roll_mode_moves_to_next_when_dte_low(self):
        # delivery_date in the past → DTE ≤ 5 → roll to next month
        contracts = [
            _make_futures_contract("TXFD7", root="TX", delivery_date=20200101),  # expired
            _make_futures_contract("TXFG7", root="TX", delivery_date=20270401),
        ]
        result = _make_result()
        idx = _make_contract_index(contracts)
        filters = FilterSpec(roll=True)
        _expand_futures("TX", "roll", {}, idx, result, filters)
        assert len(result.symbols) == 1
        assert result.symbols[0]["code"] == "TXFG7"

    def test_filters_months_override_month_token(self):
        result = _make_result()
        idx = self._make_index()
        filters = FilterSpec(months=["next"])
        _expand_futures("TX", "front", {}, idx, result, filters)
        assert len(result.symbols) == 1
        assert result.symbols[0]["code"] == "TXFG7"


# ---------------------------------------------------------------------------
# _expand_options
# ---------------------------------------------------------------------------


class TestExpandOptions:
    def _make_index(self, extra_contracts: list[dict] | None = None) -> ContractIndex:
        strikes = [17500.0, 17750.0, 18000.0, 18250.0, 18500.0]
        contracts: list[dict] = []
        for k in strikes:
            contracts.append(
                _make_option_contract(
                    f"TXO{int(k)}C7",
                    root="TXO",
                    strike=k,
                    right="C",
                    reference=18000.0,
                    delivery_date=20270301,
                )
            )
            contracts.append(
                _make_option_contract(
                    f"TXO{int(k)}P7",
                    root="TXO",
                    strike=k,
                    right="P",
                    reference=18000.0,
                    delivery_date=20270301,
                )
            )
        if extra_contracts:
            contracts.extend(extra_contracts)
        return _make_contract_index(contracts)

    def test_atm_no_offset_selects_atm_call_and_put(self):
        result = _make_result()
        idx = self._make_index()
        _expand_options("TXO", "front", "ATM", {}, idx, result)
        codes = {s["code"] for s in result.symbols}
        assert "TXO18000C7" in codes
        assert "TXO18000P7" in codes

    def test_atm_offset_selects_range(self):
        result = _make_result()
        idx = self._make_index()
        _expand_options("TXO", "front", "ATM+1", {}, idx, result)
        # ATM=18000, offset=1 → strikes 17750 to 18250 (3 strikes × C+P = 6 contracts)
        assert len(result.symbols) == 6

    def test_otm_selects_call_above_and_put_below(self):
        result = _make_result()
        idx = self._make_index()
        _expand_options("TXO", "front", "OTM+1", {}, idx, result)
        codes = {s["code"] for s in result.symbols}
        # OTM calls: 18250; OTM puts: 17750
        assert "TXO18250C7" in codes
        assert "TXO17750P7" in codes
        # ATM strike should NOT be selected as OTM
        assert "TXO18000C7" not in codes
        assert "TXO18000P7" not in codes

    def test_no_contract_index_produces_error(self):
        result = _make_result()
        _expand_options("TXO", "front", "ATM", {}, None, result)
        assert len(result.errors) == 1
        assert "contract cache" in result.errors[0]

    def test_unknown_root_produces_error(self):
        result = _make_result()
        idx = self._make_index()
        _expand_options("MISSING", "front", "ATM", {}, idx, result)
        assert len(result.errors) == 1
        assert "MISSING" in result.errors[0]

    def test_unknown_month_produces_error(self):
        result = _make_result()
        idx = self._make_index()
        _expand_options("TXO", "middle", "ATM", {}, idx, result)
        assert len(result.errors) >= 1

    def test_out_of_range_month_produces_error(self):
        result = _make_result()
        idx = self._make_index()
        # Only front month in index, "far" (idx=2) out of range
        _expand_options("TXO", "far", "ATM", {}, idx, result)
        assert len(result.errors) >= 1

    def test_unknown_selector_produces_error(self):
        result = _make_result()
        idx = self._make_index()
        _expand_options("TXO", "front", "ITM+1", {}, idx, result)
        assert len(result.errors) >= 1
        assert "Unknown option selector" in result.errors[0]

    def test_reference_fallback_to_median_strike(self):
        # Contracts without reference_price → median strike used
        strikes = [17000.0, 18000.0, 19000.0]
        contracts: list[dict] = []
        for k in strikes:
            contracts.append(
                _make_option_contract(f"TXO{int(k)}C7", root="TXO", strike=k, right="C", delivery_date=20270301)
            )
            contracts.append(
                _make_option_contract(f"TXO{int(k)}P7", root="TXO", strike=k, right="P", delivery_date=20270301)
            )
        result = _make_result()
        idx = _make_contract_index(contracts)
        _expand_options("TXO", "front", "ATM", {}, idx, result)
        assert len(result.warnings) >= 1
        assert "median strike" in result.warnings[0]

    def test_otm_offset_two_selects_two_strikes_each_side(self):
        result = _make_result()
        idx = self._make_index()
        _expand_options("TXO", "front", "OTM+2", {}, idx, result)
        codes = {s["code"] for s in result.symbols}
        # OTM calls: 18250, 18500; OTM puts: 17750, 17500
        assert "TXO18250C7" in codes
        assert "TXO18500C7" in codes
        assert "TXO17750P7" in codes
        assert "TXO17500P7" in codes

    def test_options_tag_applied(self):
        result = _make_result()
        idx = self._make_index()
        _expand_options("TXO", "front", "ATM", {}, idx, result)
        for sym in result.symbols:
            assert "options" in sym["tags"]

    def test_exchange_set_on_symbols(self):
        # build_entry defaults alpha codes to "FUT", setdefault("OPT") has no effect;
        # contracts without an explicit exchange field get defaulted by build_entry
        result = _make_result()
        idx = self._make_index()
        _expand_options("TXO", "front", "ATM", {}, idx, result)
        for sym in result.symbols:
            assert "exchange" in sym

    def test_empty_selector_result_produces_error(self):
        # OTM+100 with only 5 strikes on each side at most → ATM is at 18000 (index 2 of 5)
        # OTM+100 would try to go 100 steps beyond the available strikes
        # For indices that are in-range, we get some; this tests the "no strikes match" logic
        # Use a selector that's valid but produces no contracts matching
        strikes = [18000.0]
        contracts = [
            _make_option_contract(
                "TXO18000C7", root="TXO", strike=18000.0, right="C", delivery_date=20270301, reference=18000.0
            ),
            _make_option_contract(
                "TXO18000P7", root="TXO", strike=18000.0, right="P", delivery_date=20270301, reference=18000.0
            ),
        ]
        result = _make_result()
        idx = _make_contract_index(contracts)
        # OTM+1 with only one strike means no OTM strikes exist → empty set
        _expand_options("TXO", "front", "OTM+1", {}, idx, result)
        assert len(result.errors) >= 1
        assert "empty set" in result.errors[0]


# ---------------------------------------------------------------------------
# expand_spec (top-level dispatch)
# ---------------------------------------------------------------------------


class TestExpandSpec:
    def _make_futures_index(self) -> ContractIndex:
        contracts = [
            _make_futures_contract("TXFD7", root="TX", delivery_date=20270301),
            _make_futures_contract("TXFG7", root="TX", delivery_date=20270401),
        ]
        return _make_contract_index(contracts)

    def _make_options_index(self) -> ContractIndex:
        strikes = [17750.0, 18000.0, 18250.0]
        contracts: list[dict] = []
        for k in strikes:
            contracts.append(
                _make_option_contract(
                    f"TXO{int(k)}C7",
                    root="TXO",
                    strike=k,
                    right="C",
                    reference=18000.0,
                    delivery_date=20270301,
                )
            )
            contracts.append(
                _make_option_contract(
                    f"TXO{int(k)}P7",
                    root="TXO",
                    strike=k,
                    right="P",
                    reference=18000.0,
                    delivery_date=20270301,
                )
            )
        return _make_contract_index(contracts)

    # Literal (no @) specs

    def test_literal_spec_known_code(self):
        contracts = [{"code": "2330", "exchange": "TSE", "name": "TSMC"}]
        result = _make_result()
        idx = _make_contract_index(contracts)
        expand_spec("2330", {}, idx, result)
        assert len(result.symbols) == 1
        assert result.symbols[0]["code"] == "2330"

    def test_literal_spec_no_contract_index(self):
        result = _make_result()
        expand_spec("2330", {}, None, result)
        assert len(result.symbols) == 1
        assert result.symbols[0]["code"] == "2330"

    def test_literal_spec_unknown_code_still_added(self):
        result = _make_result()
        idx = self._make_futures_index()
        expand_spec("UNKNOWN", {}, idx, result)
        # Code not in by_code but still added as-is
        assert len(result.symbols) == 1
        assert result.symbols[0]["code"] == "UNKNOWN"

    # FUT@ specs

    def test_fut_at_spec_expands_front(self):
        result = _make_result()
        idx = self._make_futures_index()
        expand_spec("FUT@TX@front", {}, idx, result)
        assert len(result.symbols) == 1
        assert result.symbols[0]["code"] == "TXFD7"

    def test_futures_at_spec_expands(self):
        result = _make_result()
        idx = self._make_futures_index()
        expand_spec("FUTURES@TX@front", {}, idx, result)
        assert len(result.symbols) == 1

    def test_fut_missing_root_produces_error(self):
        result = _make_result()
        idx = self._make_futures_index()
        expand_spec("FUT@", {}, idx, result)
        assert len(result.errors) >= 1
        assert "missing root" in result.errors[0]

    def test_fut_roll_token_sets_roll_flag(self):
        result = _make_result()
        idx = self._make_futures_index()
        expand_spec("FUT@TX@roll", {}, idx, result)
        # roll with far future delivery date → stays on front
        assert len(result.symbols) == 1
        assert result.symbols[0]["code"] == "TXFD7"

    # OPT@ specs

    def test_opt_at_spec_expands_atm(self):
        result = _make_result()
        idx = self._make_options_index()
        expand_spec("OPT@TXO@front@ATM", {}, idx, result)
        codes = {s["code"] for s in result.symbols}
        assert "TXO18000C7" in codes
        assert "TXO18000P7" in codes

    def test_options_at_spec_expands(self):
        result = _make_result()
        idx = self._make_options_index()
        expand_spec("OPTIONS@TXO@front@ATM", {}, idx, result)
        assert len(result.symbols) >= 2

    def test_option_at_spec_expands(self):
        result = _make_result()
        idx = self._make_options_index()
        expand_spec("OPTION@TXO@front@ATM", {}, idx, result)
        assert len(result.symbols) >= 2

    def test_opt_missing_root_produces_error(self):
        result = _make_result()
        idx = self._make_options_index()
        expand_spec("OPT@", {}, idx, result)
        assert len(result.errors) >= 1
        assert "missing root" in result.errors[0]

    def test_opt_defaults_to_near_and_atm(self):
        # OPT@TXO without month or selector → defaults to near/ATM
        result = _make_result()
        idx = self._make_options_index()
        expand_spec("OPT@TXO", {}, idx, result)
        assert len(result.symbols) >= 2

    # SYNTH@ specs

    def test_synth_at_spec_creates_symbols(self):
        result = _make_result()
        expand_spec("SYNTH@5", {}, None, result)
        assert len(result.symbols) == 5

    def test_stress_at_spec_creates_symbols(self):
        result = _make_result()
        expand_spec("STRESS@3", {}, None, result)
        assert len(result.symbols) == 3

    def test_synth_missing_count_produces_error(self):
        result = _make_result()
        expand_spec("SYNTH@", {}, None, result)
        assert len(result.errors) >= 1
        assert "count" in result.errors[0]

    def test_synth_invalid_count_produces_error(self):
        result = _make_result()
        expand_spec("SYNTH@abc", {}, None, result)
        assert len(result.errors) >= 1
        assert "Invalid synthetic count" in result.errors[0]

    # Implicit futures (root@month, no FUT prefix)

    def test_implicit_futures_spec(self):
        result = _make_result()
        idx = self._make_futures_index()
        expand_spec("TX@front", {}, idx, result)
        assert len(result.symbols) == 1
        assert result.symbols[0]["code"] == "TXFD7"

    def test_implicit_futures_roll(self):
        result = _make_result()
        idx = self._make_futures_index()
        expand_spec("TX@roll", {}, idx, result)
        # roll with far-future delivery → stays on front
        assert len(result.symbols) == 1

    def test_unknown_at_spec_with_single_part_produces_error(self):
        result = _make_result()
        # "@" present but splitting by "@" gives only one non-empty part
        # e.g. "@" alone → parts empty
        expand_spec("@", {}, None, result)
        assert len(result.errors) >= 1

    def test_empty_at_spec_produces_error(self):
        # "@@@" → all parts empty after filtering
        result = _make_result()
        expand_spec("@@@", {}, None, result)
        assert len(result.errors) >= 1

    def test_implicit_futures_next_month(self):
        result = _make_result()
        idx = self._make_futures_index()
        expand_spec("TX@next", {}, idx, result)
        assert len(result.symbols) == 1
        assert result.symbols[0]["code"] == "TXFG7"

    def test_filter_token_parsed_in_fut_spec(self):
        # FUT@TX@front@oi>=100 — filter token parsed without error
        # No metrics so contract will be dropped (OI not found for 100% contracts)
        # But no parse error should occur
        result = _make_result()
        idx = self._make_futures_index()
        expand_spec("FUT@TX@front@oi>=100", {}, idx, result)
        # Might produce metrics error, but no parse error — errors list contains
        # only metrics-related errors, not parse errors
        parse_errors = [e for e in result.errors if "parse" in e.lower() or "syntax" in e.lower()]
        assert len(parse_errors) == 0

    def test_opt_with_atm_offset_selector(self):
        result = _make_result()
        idx = self._make_options_index()
        expand_spec("OPT@TXO@front@ATM+1", {}, idx, result)
        # ATM+1 → 3 strikes × C+P = 6
        assert len(result.symbols) == 6
