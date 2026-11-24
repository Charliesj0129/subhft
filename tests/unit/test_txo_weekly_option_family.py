"""``OPT@TXO_WEEKLY@near``: a virtual option root over the weekly roots (TX1..TX5, TXU..TXZ).

Pins how the family expands (nearest weekly across roots, never the monthly TXO), how its strike window is centred
(on the nearest TXF reference, not the median strike), what its entries carry, that a family with nothing listed is a
warning (so a missing weekly cannot refuse the monthly chain and the futures with it) while a plain root keeps its
error, and the 404-option-code budget of the real ``config/symbols.list`` rules.

All fixtures are synthetic ``ContractIndex`` rows with far-future expiries and a pinned ``as_of``; no real
``contracts.json`` is read.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from hft_platform.config._symbols_expansion import (
    OPTION_ROOT_FAMILIES,
    TXO_WEEKLY_ROOTS,
    UNDERLYING_FUTURE_ROOT,
    _expand_options,
    build_entry,
    expand_spec,
    pick_underlying_price,
)
from hft_platform.config._symbols_types import ContractIndex, SymbolBuildResult
from hft_platform.config.symbols import build_symbols
from hft_platform.feed_adapter.shioaji import quote_connection_pool as qcp
from hft_platform.feed_adapter.shioaji.limits import DEFAULT_MAX_SUBSCRIPTIONS_PER_CONN

_AS_OF = date(2030, 3, 18)  # a Monday; every expiry below is on or after it
_MONTHLY_EXPIRY = "2030/03/20"  # TXO: the third Wednesday, so it expires before every weekly below
_NEAR_TXF_EXPIRY = "2030/03/20"
_FAR_TXF_EXPIRY = "2030/04/17"

# Two weeks of the same three roots. In the first the Wednesday weekly (TX1) is nearer than the Friday one (TXU); in
# the second the Friday weekly is nearer. Whichever root comes first in TXO_WEEKLY_ROOTS must not decide it.
_LAYOUTS = {
    "wed_weekly_nearer": {"TX1": "2030/03/27", "TXU": "2030/03/29"},
    "fri_weekly_nearer": {"TXU": "2030/03/22", "TX1": "2030/03/27"},
}
_NEAREST = {"wed_weekly_nearer": ("TX1", "2030/03/27"), "fri_weekly_nearer": ("TXU", "2030/03/22")}
_SECOND = {"wed_weekly_nearer": ("TXU", "2030/03/29"), "fri_weekly_nearer": ("TX1", "2030/03/27")}

_WIDE_STRIKES = list(range(15000, 23001, 500))  # 17 strikes; the middle one is 19000
_TXF_REFERENCE = 18000.0  # not the middle strike


def _option(root: str, strike: int, right: str, expiry: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "code": f"{root}{strike}{right}{expiry[5:7]}{expiry[8:10]}",
        "type": "option",
        "root": root,
        "right": right,
        "strike": strike,
        "delivery_date": expiry,
        "reference": 150,  # an option's reference is its premium, never the underlying
    }
    row.update(extra)
    return row


def _chain(root: str, expiry: str, strikes: list[int]) -> list[dict[str, Any]]:
    return [_option(root, strike, right, expiry) for strike in strikes for right in ("C", "P")]


def _txf(code: str, expiry: str, reference: float | None) -> dict[str, Any]:
    row: dict[str, Any] = {"code": code, "type": "future", "root": "TXF", "delivery_date": expiry}
    if reference is not None:
        row["reference"] = reference
    return row


def _futures() -> list[dict[str, Any]]:
    """The nearest TXF carries the reference to centre on; the far one must be ignored."""
    return [
        _txf("TXFC0", _NEAR_TXF_EXPIRY, _TXF_REFERENCE),
        _txf("TXFD0", _FAR_TXF_EXPIRY, 20000.0),
    ]


def _index(rows: list[dict[str, Any]], **kwargs: Any) -> ContractIndex:
    return ContractIndex(contracts=rows, as_of=_AS_OF, **kwargs)


def _layout_rows(layout: str, strikes: list[int]) -> list[dict[str, Any]]:
    rows = _chain("TXO", _MONTHLY_EXPIRY, strikes)
    for root, expiry in _LAYOUTS[layout].items():
        rows += _chain(root, expiry, strikes)
    return rows


def _expand(spec: str, index: ContractIndex | None) -> SymbolBuildResult:
    result = SymbolBuildResult()
    expand_spec(spec, {}, index, result)
    return result


def _roots_of(result: SymbolBuildResult, index: ContractIndex) -> set[str]:
    return {index.by_code[entry["code"]]["root"] for entry in result.symbols}


def _strike_rights(result: SymbolBuildResult) -> set[tuple[int, str]]:
    return {(int(entry["strike"]), entry["right"]) for entry in result.symbols}


# ---------------------------------------------------------------------------
# 1. The nearest weekly across roots, never the monthly TXO
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layout", list(_LAYOUTS))
def test_weekly_near_picks_the_nearest_expiring_weekly_across_roots(layout):
    strikes = [17500, 18000, 18500]
    index = _index(_layout_rows(layout, strikes) + _futures())

    result = _expand("OPT@TXO_WEEKLY@near@ATM+/-1", index)

    expected_root, expected_expiry = _NEAREST[layout]
    assert result.errors == []
    assert len(result.symbols) == 6  # 3 strikes x both rights
    assert _roots_of(result, index) == {expected_root}
    assert {entry["expiry"] for entry in result.symbols} == {expected_expiry}


@pytest.mark.parametrize("layout", list(_LAYOUTS))
def test_weekly_near_never_includes_the_monthly_txo_even_when_it_expires_first(layout):
    index = _index(_layout_rows(layout, [17500, 18000, 18500]) + _futures())
    weekly_expiries = _LAYOUTS[layout].values()
    assert all(_MONTHLY_EXPIRY < expiry for expiry in weekly_expiries)  # the premise: TXO is the nearest option

    result = _expand("OPT@TXO_WEEKLY@near@ATM+/-1", index)

    assert result.symbols
    assert "TXO" not in _roots_of(result, index)
    assert not any(entry["code"].startswith("TXO") for entry in result.symbols)
    assert _MONTHLY_EXPIRY not in {entry["expiry"] for entry in result.symbols}


@pytest.mark.parametrize("layout", list(_LAYOUTS))
def test_weekly_next_is_the_second_nearest_weekly_not_the_monthly_txo(layout):
    index = _index(_layout_rows(layout, [17500, 18000, 18500]) + _futures())

    result = _expand("OPT@TXO_WEEKLY@next@ATM+/-1", index)

    expected_root, expected_expiry = _SECOND[layout]
    assert result.errors == []
    assert _roots_of(result, index) == {expected_root}
    assert {entry["expiry"] for entry in result.symbols} == {expected_expiry}


def test_monthly_near_rule_is_unchanged_and_never_includes_a_weekly():
    index = _index(_layout_rows("fri_weekly_nearer", [17500, 18000, 18500]) + _futures())

    result = _expand("OPT@TXO@near@ATM+/-1", index)

    assert result.errors == []
    assert _roots_of(result, index) == {"TXO"}
    assert {entry["expiry"] for entry in result.symbols} == {_MONTHLY_EXPIRY}


def test_weekly_family_is_the_declared_weekly_roots_and_excludes_the_monthly_root():
    assert OPTION_ROOT_FAMILIES["TXO_WEEKLY"] == TXO_WEEKLY_ROOTS
    assert "TXO" not in TXO_WEEKLY_ROOTS
    assert {"TX1", "TX2", "TXU", "TXV"} <= set(TXO_WEEKLY_ROOTS)  # the roots seen in the 2026-08-31 cache
    assert len(set(TXO_WEEKLY_ROOTS)) == len(TXO_WEEKLY_ROOTS)


# ---------------------------------------------------------------------------
# 2. The window is centred on the nearest TXF reference, N strikes each side
# ---------------------------------------------------------------------------


def test_weekly_window_is_centred_on_the_nearest_txf_reference_not_the_median_strike():
    index = _index(_chain("TX1", "2030/03/27", _WIDE_STRIKES) + _futures())
    median_strike = _WIDE_STRIKES[len(_WIDE_STRIKES) // 2]
    assert median_strike != _TXF_REFERENCE  # the premise

    result = _expand("OPT@TXO_WEEKLY@near@ATM+/-2", index)

    assert _strike_rights(result) == {
        (strike, right) for strike in (17000, 17500, 18000, 18500, 19000) for right in "CP"
    }
    assert not any("median strike" in warning for warning in result.warnings)


@pytest.mark.parametrize("each_side", [1, 3, 4])
def test_weekly_atm_window_selects_n_strikes_each_side_for_both_rights(each_side):
    index = _index(_chain("TXU", "2030/03/22", _WIDE_STRIKES) + _futures())

    result = _expand(f"OPT@TXO_WEEKLY@near@ATM+/-{each_side}", index)

    centre = int(_TXF_REFERENCE)
    expected_strikes = {centre + step * 500 for step in range(-each_side, each_side + 1)}
    assert _strike_rights(result) == {(strike, right) for strike in expected_strikes for right in "CP"}
    assert len(result.symbols) == 2 * (2 * each_side + 1)


def test_weekly_window_ignores_the_far_txf_expiry_and_an_alias_row_without_a_reference():
    futures = [
        _txf("TXFR1", _NEAR_TXF_EXPIRY, None),  # continuous alias sharing the nearest expiry, no price
        _txf("TXFC0", _NEAR_TXF_EXPIRY, _TXF_REFERENCE),
        _txf("TXFD0", _FAR_TXF_EXPIRY, 22000.0),
    ]
    index = _index(_chain("TX1", "2030/03/27", _WIDE_STRIKES) + futures)

    result = _expand("OPT@TXO_WEEKLY@near@ATM", index)

    assert _strike_rights(result) == {(int(_TXF_REFERENCE), "C"), (int(_TXF_REFERENCE), "P")}


@pytest.mark.parametrize("root", ["TXO_WEEKLY", *TXO_WEEKLY_ROOTS])
def test_underlying_price_for_a_weekly_root_is_the_nearest_txf_reference(root):
    index = _index(_futures())

    price, source = pick_underlying_price(root, [], index)

    assert UNDERLYING_FUTURE_ROOT[root] == "TXF"
    assert (price, source) == (_TXF_REFERENCE, "TXF:TXFC0")


def test_strict_weekly_build_refuses_to_guess_without_a_txf_reference():
    index = _index(_chain("TX1", "2030/03/27", _WIDE_STRIKES) + [_txf("TXFC0", _NEAR_TXF_EXPIRY, None)])
    index.require_underlying = True

    result = _expand("OPT@TXO_WEEKLY@near@ATM+/-2", index)

    assert result.symbols == []
    assert any("refusing to guess" in error and "TXF" in error for error in result.errors)


def test_strict_weekly_build_accepts_a_txf_reference():
    index = _index(_chain("TX1", "2030/03/27", _WIDE_STRIKES) + _futures())
    index.require_underlying = True

    result = _expand("OPT@TXO_WEEKLY@near@ATM+/-2", index)

    assert result.errors == []
    assert len(result.symbols) == 10


# ---------------------------------------------------------------------------
# 3. What a weekly entry carries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layout", list(_LAYOUTS))
def test_weekly_entries_carry_underlying_tx_point_value_50_and_the_weekly_expiry(layout):
    index = _index(_layout_rows(layout, [17500, 18000, 18500]) + _futures())

    result = _expand("OPT@TXO_WEEKLY@near@ATM+/-1", index)

    _, expected_expiry = _NEAREST[layout]
    assert len(result.symbols) == 6
    for entry in result.symbols:
        assert entry["underlying"] == "TX"
        assert entry["point_value"] == 50
        assert type(entry["point_value"]) is int
        assert entry["expiry"] == expected_expiry
        assert entry["price_scale"] == 10000
        assert entry["right"] in {"C", "P"}


@pytest.mark.parametrize("root", TXO_WEEKLY_ROOTS)
def test_every_weekly_root_entry_has_underlying_tx_and_point_value_50(root):
    contract = {
        "code": f"{root}18000C0",
        "type": "option",
        "right": "C",
        "strike": 18000,
        "delivery_date": "2030/03/27",
    }
    result = SymbolBuildResult()

    entry = build_entry(contract["code"], {}, contract, result)

    assert entry is not None
    assert entry["underlying"] == "TX"
    assert entry["point_value"] == 50
    assert entry["expiry"] == "2030/03/27"


# ---------------------------------------------------------------------------
# 4. A family with nothing listed warns; a plain root still errors
# ---------------------------------------------------------------------------


def test_weekly_family_with_no_listed_contract_warns_and_adds_no_error():
    index = _index(_chain("TXO", _MONTHLY_EXPIRY, [17500, 18000, 18500]) + _futures())

    result = _expand("OPT@TXO_WEEKLY@near@ATM+/-1", index)

    assert result.errors == []
    assert result.symbols == []
    assert any("No option contracts found for root TXO_WEEKLY" in warning for warning in result.warnings)


def test_weekly_family_whose_contracts_have_all_expired_warns_and_adds_no_error():
    expired = "2030/03/13"  # before _AS_OF
    rows = _chain("TX1", expired, [17500, 18000, 18500]) + _chain("TXU", expired, [17500, 18000, 18500])
    index = _index(rows + _futures())

    result = _expand("OPT@TXO_WEEKLY@near@ATM+/-1", index)

    assert result.errors == []
    assert result.symbols == []
    assert any("No active (non-expired) option contracts for root TXO_WEEKLY" in w for w in result.warnings)


def test_monthly_rule_still_expands_in_a_build_whose_weekly_rule_finds_nothing(tmp_path):
    list_path = tmp_path / "symbols.list"
    list_path.write_text(
        "OPT@TXO_WEEKLY@near@ATM+/-1 exchange=OPT tags=options|near_week|atm\n"
        "OPT@TXO@near@ATM+/-1 exchange=OPT tags=options|near_month|atm\n",
        encoding="utf-8",
    )
    index = _index(_chain("TXO", _MONTHLY_EXPIRY, [17500, 18000, 18500]) + _futures())

    result = build_symbols(str(list_path), index)

    assert result.errors == []
    assert result.ok()
    assert len(result.symbols) == 6
    assert {entry["code"][:3] for entry in result.symbols} == {"TXO"}
    assert any("TXO_WEEKLY" in warning for warning in result.warnings)


def test_monthly_rule_still_expands_when_every_weekly_contract_has_expired(tmp_path):
    list_path = tmp_path / "symbols.list"
    list_path.write_text(
        "OPT@TXO_WEEKLY@near@ATM+/-1 exchange=OPT\nOPT@TXO@near@ATM+/-1 exchange=OPT\n",
        encoding="utf-8",
    )
    rows = _chain("TX1", "2030/03/13", [17500, 18000, 18500]) + _chain("TXO", _MONTHLY_EXPIRY, [17500, 18000, 18500])
    index = _index(rows + _futures())

    result = build_symbols(str(list_path), index)

    assert result.errors == []
    assert len(result.symbols) == 6
    assert {entry["expiry"] for entry in result.symbols} == {_MONTHLY_EXPIRY}


def test_weekly_family_without_a_contract_cache_is_still_an_error():
    """No cache at all is a broken build, not a quiet week: the family downgrade covers only an empty family."""
    result = SymbolBuildResult()

    _expand_options("TXO_WEEKLY", "near", "ATM+1", {}, None, result)

    assert len(result.errors) == 1
    assert "contract cache" in result.errors[0]


def test_plain_root_with_no_contracts_still_errors():
    index = _index(_chain("TXO", _MONTHLY_EXPIRY, [17500, 18000, 18500]) + _futures())

    result = _expand("OPT@MISSING@near@ATM+/-1", index)

    assert result.symbols == []
    assert not any("MISSING" in warning for warning in result.warnings)  # an error, not a downgraded warning
    assert result.errors == ["No option contracts found for root MISSING"]


def test_plain_root_whose_contracts_have_all_expired_still_errors():
    index = _index(_chain("TXO", "2030/03/13", [17500, 18000, 18500]) + _futures())

    result = _expand("OPT@TXO@near@ATM+/-1", index)

    assert result.symbols == []
    assert result.errors == ["No active (non-expired) option contracts for root TXO"]


def test_a_weekly_broker_root_named_directly_is_a_plain_root_and_still_errors():
    """Only the TXO_WEEKLY family is downgraded; naming TX1 itself with nothing listed under it is an error."""
    index = _index(_chain("TXU", "2030/03/22", [17500, 18000, 18500]) + _futures())

    result = _expand("OPT@TX1@near@ATM+/-1", index)

    assert result.symbols == []
    assert result.errors == ["No option contracts found for root TX1"]


# ---------------------------------------------------------------------------
# 7. The real config/symbols.list rules over a synthetic cache
# ---------------------------------------------------------------------------

_REAL_LIST = Path(__file__).resolve().parents[2] / "config" / "symbols.list"
_WINDOW = 50
_STRIKE_STEP = 100
_BUILD_REFERENCE = 45000
# 161 listed strikes, so +/-50 around the reference never touches the edge of the chain, and the middle strike (44000)
# is not the reference: a window centred on the median would be off by 10 strikes.
_BUILD_STRIKES = list(range(36000, 52001, _STRIKE_STEP))
_BUILD_AS_OF = date(2030, 3, 18)
_BUILD_WEEKLY_EXPIRY = "2030/03/27"  # TX1; the nearest weekly (TXU 03/29 and TX2 04/03 are later)
_BUILD_MONTHLY_EXPIRY = "2030/03/20"  # TXO current month


def _real_list_cache() -> ContractIndex:
    futures: list[dict[str, Any]] = []
    for root, expiries in (
        ("TXF", ("2030/03/20", "2030/04/17", "2030/05/15")),
        ("MXF", ("2030/03/20", "2030/04/17")),
        ("TMF", ("2030/03/20", "2030/04/17")),
        ("EXF", ("2030/03/20",)),
    ):
        for expiry in expiries:
            futures.append(
                {
                    "code": f"{root}{expiry[5:7]}",
                    "type": "future",
                    "root": root,
                    "delivery_date": expiry,
                    "reference": _BUILD_REFERENCE if root == "TXF" else 1.0,
                }
            )
    options: list[dict[str, Any]] = []
    options += _chain("TX1", _BUILD_WEEKLY_EXPIRY, _BUILD_STRIKES)
    options += _chain("TXU", "2030/03/29", _BUILD_STRIKES)
    options += _chain("TX2", "2030/04/03", _BUILD_STRIKES)
    options += _chain("TXO", _BUILD_MONTHLY_EXPIRY, _BUILD_STRIKES)
    options += _chain("TXO", "2030/04/17", _BUILD_STRIKES)
    return ContractIndex(contracts=[*futures, *options], as_of=_BUILD_AS_OF)


def test_real_symbols_list_gives_404_option_codes_for_the_weekly_and_current_month_windows():
    index = _real_list_cache()

    result = build_symbols(str(_REAL_LIST), index)

    option_entries = [entry for entry in result.symbols if index.by_code.get(entry["code"], {}).get("type") == "option"]
    weekly = [e for e in option_entries if index.by_code[e["code"]]["root"] in TXO_WEEKLY_ROOTS]
    monthly = [e for e in option_entries if index.by_code[e["code"]]["root"] == "TXO"]
    assert result.errors == []
    assert len(option_entries) == 2 * (2 * _WINDOW + 1) * 2 == 404
    assert len(weekly) == 202
    assert len(monthly) == 202
    assert {e["expiry"] for e in weekly} == {_BUILD_WEEKLY_EXPIRY}
    assert {e["expiry"] for e in monthly} == {_BUILD_MONTHLY_EXPIRY}
    for group in (weekly, monthly):
        strikes = {int(e["strike"]) for e in group}
        assert min(strikes) == _BUILD_REFERENCE - _WINDOW * _STRIKE_STEP
        assert max(strikes) == _BUILD_REFERENCE + _WINDOW * _STRIKE_STEP
        assert len(strikes) == 2 * _WINDOW + 1
        assert {e["right"] for e in group} == {"C", "P"}


def test_real_symbols_list_universe_fits_the_quote_connection_budget():
    result = build_symbols(str(_REAL_LIST), _real_list_cache())

    assert result.errors == []
    assert len(result.symbols) <= qcp._MAX_QUOTE_CONNECTIONS * DEFAULT_MAX_SUBSCRIPTIONS_PER_CONN
