"""The option-chain ATM coverage rule judges the subscribed weekly expiries as well as the monthly TXO.

``chain_coverage`` takes one root or several; the pool passes the monthly root plus every weekly root so that
``option_chain_atm_coverage{expiry}`` has a series for the nearest weekly and for the current month, each judged on its
own strikes against the same nearest-TXF reference. Synthetic rows only; the pool is built the way
``test_option_chain_coverage`` builds it (no SDK call).
"""

from __future__ import annotations

import unittest.mock as mock
from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from hft_platform.config._symbols_expansion import TXO_WEEKLY_ROOTS
from hft_platform.config._symbols_types import ContractIndex
from hft_platform.config.option_chain_coverage import ExpiryCoverage, chain_coverage
from hft_platform.feed_adapter.shioaji import quote_connection_pool as qcp
from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

_REFERENCE = 20000.0
_WEEKLY = "2030/03/27"  # nearer
_WEEKLY_ISO = "2030-03-27"
_MONTHLY = "2030/04/17"
_MONTHLY_ISO = "2030-04-17"

_COVERING = [19400, 19800, 20000, 20200, 20600]  # reaches F-500 and F+500
_STOPS_BELOW = [19000, 19400, 19800, 20000]  # nothing above F: no out-of-the-money calls


def _row(root: str, strike: int, expiry: str, right: str = "C") -> dict[str, Any]:
    return {
        "code": f"{root}{strike}{right}{expiry[5:7]}{expiry[8:10]}",
        "type": "option",
        "root": root,
        "right": right,
        "strike": str(strike),
        "delivery_date": expiry,
        "reference": "150",  # the option's premium, never the underlying
    }


def _rows(root: str, expiry: str, strikes: list[int]) -> dict[str, dict[str, Any]]:
    rows = [_row(root, strike, expiry) for strike in strikes]
    return {row["code"]: row for row in rows}


def _mixed() -> dict[str, dict[str, Any]]:
    """A covering weekly (TX1) chain and a monthly (TXO) chain that stops below the future."""
    return {**_rows("TX1", _WEEKLY, _COVERING), **_rows("TXO", _MONTHLY, _STOPS_BELOW)}


# ── the pure rule with several roots ──────────────────────────────────────


def test_a_tuple_of_roots_judges_the_weekly_and_the_monthly_expiry_separately():
    rows = _mixed()

    result = chain_coverage(list(rows), rows, _REFERENCE, root=("TXO", *TXO_WEEKLY_ROOTS))

    assert result == [
        ExpiryCoverage(_WEEKLY_ISO, 5, 19400.0, 20600.0, _REFERENCE, True),
        ExpiryCoverage(_MONTHLY_ISO, 4, 19000.0, 20000.0, _REFERENCE, False),
    ]
    assert [item.gauge_value for item in result] == [1, 0]


def test_a_weekly_chain_that_stops_below_the_future_is_not_covered_while_the_monthly_one_is():
    rows = {**_rows("TXU", _WEEKLY, _STOPS_BELOW), **_rows("TXO", _MONTHLY, _COVERING)}

    result = chain_coverage(list(rows), rows, _REFERENCE, root=("TXO", *TXO_WEEKLY_ROOTS))

    assert [(item.expiry, item.covered) for item in result] == [(_WEEKLY_ISO, False), (_MONTHLY_ISO, True)]


def test_several_weekly_roots_at_different_expiries_stay_separate_expiries():
    rows = {**_rows("TXU", "2030/03/22", _STOPS_BELOW), **_rows("TX1", _WEEKLY, _COVERING)}

    result = chain_coverage(list(rows), rows, _REFERENCE, root=TXO_WEEKLY_ROOTS)

    assert [(item.expiry, item.covered) for item in result] == [("2030-03-22", False), (_WEEKLY_ISO, True)]


def test_a_single_string_root_still_works_and_ignores_weekly_rows():
    rows = _mixed()

    result = chain_coverage(list(rows), rows, _REFERENCE)  # the default root, "TXO"

    assert [(item.expiry, item.contracts, item.covered) for item in result] == [(_MONTHLY_ISO, 4, False)]


def test_a_single_weekly_string_root_selects_only_that_root():
    rows = {**_mixed(), **_rows("TXU", "2030/03/29", _COVERING)}

    result = chain_coverage(list(rows), rows, _REFERENCE, root="TX1")

    assert [(item.expiry, item.contracts) for item in result] == [(_WEEKLY_ISO, 5)]


@pytest.mark.parametrize(
    "as_roots",
    [
        pytest.param(lambda roots: tuple(roots), id="tuple"),
        pytest.param(lambda roots: list(roots), id="list"),
        pytest.param(lambda roots: frozenset(roots), id="frozenset"),
        pytest.param(lambda roots: (r for r in roots), id="generator"),
    ],
)
def test_roots_may_be_any_iterable_of_strings(as_roots: Callable[[Iterable[str]], Iterable[str]]):
    rows = _mixed()

    result = chain_coverage(list(rows), rows, _REFERENCE, root=as_roots(["TXO", "TX1"]))

    assert [item.expiry for item in result] == [_WEEKLY_ISO, _MONTHLY_ISO]


def test_root_matching_ignores_case_in_both_the_roots_and_the_rows():
    rows = _mixed()
    rows["TX1LOWER"] = {**_row("tx1", 20000, "2030/03/29"), "code": "TX1LOWER"}

    result = chain_coverage(list(rows), rows, _REFERENCE, root=("txo", "tx1"))

    assert [(item.expiry, item.contracts) for item in result] == [
        (_WEEKLY_ISO, 5),
        ("2030-03-29", 1),
        (_MONTHLY_ISO, 4),
    ]


def test_an_empty_root_collection_matches_nothing_not_everything():
    rows = _mixed()

    assert chain_coverage(list(rows), rows, _REFERENCE, root=()) == []


def test_weekly_expiries_are_unknown_not_uncovered_without_a_reference_price():
    rows = _mixed()

    result = chain_coverage(list(rows), rows, None, root=("TXO", *TXO_WEEKLY_ROOTS))

    assert [(item.expiry, item.covered, item.gauge_value) for item in result] == [
        (_WEEKLY_ISO, None, -1),
        (_MONTHLY_ISO, None, -1),
    ]


def test_a_root_that_is_not_in_the_collection_is_left_out():
    rows = {**_mixed(), **_rows("MXO", "2030/03/29", _COVERING)}

    result = chain_coverage(list(rows), rows, _REFERENCE, root=("TXO", *TXO_WEEKLY_ROOTS))

    assert [item.expiry for item in result] == [_WEEKLY_ISO, _MONTHLY_ISO]


# ── the pool gauge ────────────────────────────────────────────────────────


def _future(price: float | None = _REFERENCE) -> dict[str, Any]:
    row: dict[str, Any] = {"code": "TXFB0", "type": "future", "root": "TXF", "delivery_date": "2030/03/20"}
    if price is not None:
        row["reference"] = price
    return row


def _make_pool(tmp_path, option_codes: list[str]) -> QuoteConnectionPool:
    sym_path = tmp_path / "symbols.yaml"
    symbols = [{"code": "TXFB0", "exchange": "TAIFEX", "group": 0}]
    symbols += [{"code": code, "exchange": "TAIFEX", "group": 0} for code in option_codes]
    sym_path.write_text(yaml.safe_dump({"symbols": symbols}))
    return QuoteConnectionPool(str(sym_path), {}, num_conns=1)


def _gauge_values() -> dict[str, float]:
    gauge = getattr(qcp, "_METRIC_ATM_COVERAGE", None)
    if gauge is None:
        return {}
    return {sample.labels["expiry"]: sample.value for family in gauge.collect() for sample in family.samples}


@pytest.fixture(autouse=True)
def _clean_gauge():
    qcp._ensure_metrics()
    gauge = getattr(qcp, "_METRIC_ATM_COVERAGE", None)
    if gauge is not None:
        gauge.clear()
    yield
    if gauge is not None:
        gauge.clear()


def _report(
    pool: QuoteConnectionPool, rows: dict[str, dict[str, Any]], *, future: dict[str, Any] | None = None, held=None
) -> list[ExpiryCoverage]:
    """Report with a facade that holds ``held`` (default: everything the pool is configured with)."""
    index = ContractIndex(contracts=[future if future is not None else _future(), *rows.values()])
    subscribed = {str(sym["code"]) for sym in pool._all_symbols} if held is None else held
    pool._clients = [SimpleNamespace(subscribed_codes=subscribed)]
    with mock.patch.object(type(pool), "_contract_index", return_value=index):
        return pool.report_option_chain_coverage()


def test_the_pool_publishes_the_subscribed_weekly_expiry_next_to_the_monthly_one(tmp_path):
    rows = _mixed()
    pool = _make_pool(tmp_path, list(rows))

    result = _report(pool, rows)

    assert [(item.expiry, item.covered) for item in result] == [(_WEEKLY_ISO, True), (_MONTHLY_ISO, False)]
    assert _gauge_values() == {_WEEKLY_ISO: 1.0, _MONTHLY_ISO: 0.0}


def test_the_pool_judges_weekly_codes_against_the_txf_reference_not_the_weekly_premium(tmp_path):
    rows = _rows("TX1", _WEEKLY, _STOPS_BELOW)  # every weekly row's reference is a 150 premium
    pool = _make_pool(tmp_path, list(rows))

    [result] = _report(pool, rows)

    assert result.reference == _REFERENCE
    assert result.covered is False
    assert _gauge_values() == {_WEEKLY_ISO: 0.0}


def test_a_pool_that_holds_only_weekly_codes_still_publishes_the_weekly_expiry(tmp_path):
    rows = _rows("TXU", _WEEKLY, _COVERING)
    pool = _make_pool(tmp_path, list(rows))

    [result] = _report(pool, rows)

    assert (result.expiry, result.covered, result.contracts) == (_WEEKLY_ISO, True, 5)
    assert _gauge_values() == {_WEEKLY_ISO: 1.0}


@pytest.mark.parametrize("weekly_root", TXO_WEEKLY_ROOTS)
def test_the_pool_publishes_a_series_for_every_weekly_root(tmp_path, weekly_root):
    rows = _rows(weekly_root, _WEEKLY, _COVERING)
    pool = _make_pool(tmp_path, list(rows))

    _report(pool, rows)

    assert _gauge_values() == {_WEEKLY_ISO: 1.0}


def test_a_weekly_strike_the_broker_refused_is_not_counted_as_covered(tmp_path):
    """Configured up to 20600 but the subscribe stopped at the future: the weekly does not reach F+500."""
    rows = _rows("TX1", _WEEKLY, _COVERING)
    pool = _make_pool(tmp_path, list(rows))
    held = {"TXFB0"} | {code for code, row in rows.items() if float(row["strike"]) <= _REFERENCE}

    [result] = _report(pool, rows, held=held)

    assert (result.max_strike, result.covered) == (20000.0, False)
    assert _gauge_values() == {_WEEKLY_ISO: 0.0}


def test_only_subscribed_weekly_codes_count_not_the_whole_weekly_cache(tmp_path):
    cache = _rows("TX1", _WEEKLY, [18000, *_COVERING, 22000])
    configured = {code: row for code, row in cache.items() if int(row["strike"]) in _COVERING}
    pool = _make_pool(tmp_path, list(configured))

    [result] = _report(pool, cache)

    assert (result.min_strike, result.max_strike, result.contracts) == (19400.0, 20600.0, 5)


def test_the_pool_publishes_minus_one_for_a_weekly_chain_when_the_future_has_no_price(tmp_path):
    rows = _rows("TX1", _WEEKLY, _COVERING)
    pool = _make_pool(tmp_path, list(rows))

    [result] = _report(pool, rows, future=_future(None))

    assert (result.covered, result.reference) == (None, None)
    assert _gauge_values() == {_WEEKLY_ISO: -1.0}


def test_a_weekly_expiry_that_rolled_off_stops_reporting(tmp_path):
    week_one = _rows("TX1", _WEEKLY, _COVERING)
    monthly = _rows("TXO", _MONTHLY, _STOPS_BELOW)
    pool = _make_pool(tmp_path, [*week_one, *monthly])
    _report(pool, {**week_one, **monthly})
    assert _gauge_values() == {_WEEKLY_ISO: 1.0, _MONTHLY_ISO: 0.0}

    next_week = _rows("TXU", "2030/04/03", _COVERING)
    pool._all_symbols = [{"code": code, "exchange": "TAIFEX", "group": 0} for code in [*next_week, *monthly]]
    _report(pool, {**next_week, **monthly})

    assert _gauge_values() == {"2030-04-03": 1.0, _MONTHLY_ISO: 0.0}


def test_roll_clock_publishes_chain_coverage_after_each_roll_check() -> None:
    """Production runs with HFT_OPTIONS_REFRESH_S=0, so the refresh thread never reports; the roll clock must."""
    import asyncio

    calls: list[str] = []
    pool: Any = SimpleNamespace(
        roll_universe=lambda: calls.append("roll"),
        _report_chain_coverage_safely=lambda: calls.append("coverage"),
    )

    async def run() -> None:
        task = asyncio.create_task(QuoteConnectionPool.run_universe_roll_clock(pool, interval_s=0))
        while calls.count("coverage") < 2:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert calls[:4] == ["roll", "coverage", "roll", "coverage"]


def test_roll_clock_still_reports_coverage_when_the_roll_raises() -> None:
    import asyncio

    calls: list[str] = []

    def failing_roll() -> None:
        calls.append("roll")
        raise RuntimeError("cache unreadable")

    pool: Any = SimpleNamespace(
        roll_universe=failing_roll,
        _report_chain_coverage_safely=lambda: calls.append("coverage"),
    )

    async def run() -> None:
        task = asyncio.create_task(QuoteConnectionPool.run_universe_roll_clock(pool, interval_s=0))
        while "coverage" not in calls:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert calls[:2] == ["roll", "coverage"]
