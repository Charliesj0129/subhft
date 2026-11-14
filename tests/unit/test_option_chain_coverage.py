"""Does the subscribed TXO chain reach the money? The pure rule, and the pool gauge built on it (F3).

The research audit found the recorded chain stopped below the futures price on most days: no skew, no term
structure, no put-call parity. These tests pin the rule that says so at subscription time, and that the pool
publishes it as ``option_chain_atm_coverage{expiry}``.
"""

from __future__ import annotations

import unittest.mock as mock
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from hft_platform.config import option_chain_coverage as occ
from hft_platform.config._symbols_types import ContractIndex
from hft_platform.config.option_chain_coverage import ExpiryCoverage, chain_coverage
from hft_platform.feed_adapter.shioaji import quote_connection_pool as qcp
from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

_EXPIRY = "2030/04/17"
_EXPIRY_ISO = "2030-04-17"
_FAR_EXPIRY = "2030/05/15"
_FAR_EXPIRY_ISO = "2030-05-15"


def _option(strike: float, *, expiry: str = _EXPIRY, right: str = "C", **extra: Any) -> dict[str, Any]:
    code = f"TXO{int(strike)}{right}{expiry[5:7]}"
    row = {
        "code": code,
        "type": "option",
        "root": "TXO",
        "right": right,
        "strike": str(strike),
        "delivery_date": expiry,
        "reference": "150",
    }
    row.update(extra)
    return row


def _chain(strikes: list[float], **kwargs: Any) -> tuple[list[str], dict[str, dict[str, Any]]]:
    rows = [_option(strike, **kwargs) for strike in strikes]
    return [row["code"] for row in rows], {row["code"]: row for row in rows}


# ── the pure rule ─────────────────────────────────────────────────────────


def test_a_chain_that_spans_the_future_both_ways_is_covered():
    codes, rows = _chain([19400, 19800, 20000, 20200, 20600])

    [result] = chain_coverage(codes, rows, 20000.0)

    assert result == ExpiryCoverage(_EXPIRY_ISO, 5, 19400.0, 20600.0, 20000.0, True)
    assert result.gauge_value == occ.COVERED == 1


def test_a_chain_that_stops_below_the_future_is_not_covered():
    """The failure the audit found: strikes up to F only, so no out-of-the-money calls."""
    codes, rows = _chain([19000, 19400, 19800, 20000])

    [result] = chain_coverage(codes, rows, 20000.0)

    assert result.covered is False
    assert result.gauge_value == occ.NOT_COVERED == 0
    assert (result.min_strike, result.max_strike) == (19000.0, 20000.0)


def test_a_chain_that_starts_above_the_money_is_not_covered_either():
    codes, rows = _chain([20000, 20400, 20800])

    [result] = chain_coverage(codes, rows, 20000.0)

    assert result.covered is False


@pytest.mark.parametrize(
    ("low", "high", "covered"),
    [
        (19500, 20500, True),  # exactly F -/+ 500 counts
        (19501, 20500, False),
        (19500, 20499, False),
    ],
)
def test_the_500_point_bounds_are_inclusive_and_exact(low, high, covered):
    codes, rows = _chain([low, 20000, high])

    [result] = chain_coverage(codes, rows, 20000.0)

    assert result.covered is covered


def test_the_half_width_is_the_callers_to_change():
    codes, rows = _chain([19900, 20100])

    assert chain_coverage(codes, rows, 20000.0)[0].covered is False
    assert chain_coverage(codes, rows, 20000.0, half_width=100)[0].covered is True


@pytest.mark.parametrize("reference", [None, 0, -5, "n/a", float("nan")])
def test_without_a_usable_underlying_price_the_chain_is_unknown_not_uncovered(reference):
    """-1 means "could not judge". Reporting 0 would raise an alarm about a chain nobody measured."""
    codes, rows = _chain([19000, 21000])

    [result] = chain_coverage(codes, rows, reference)

    assert result.covered is None
    assert result.gauge_value == occ.UNKNOWN == -1
    assert result.reference is None


def test_expiries_are_separate_and_nearest_first():
    near_codes, near_rows = _chain([19400, 20600], expiry=_EXPIRY)
    far_codes, far_rows = _chain([19900, 20100], expiry=_FAR_EXPIRY)

    result = chain_coverage(far_codes + near_codes, {**far_rows, **near_rows}, 20000.0)

    assert [(item.expiry, item.covered) for item in result] == [(_EXPIRY_ISO, True), (_FAR_EXPIRY_ISO, False)]


def test_calls_and_puts_of_one_strike_count_once_toward_the_span_but_twice_as_contracts():
    call, put = _option(20000, right="C"), _option(20000, right="P")

    [result] = chain_coverage([call["code"], put["code"]], {call["code"]: call, put["code"]: put}, 20000.0)

    assert result.contracts == 2
    assert (result.min_strike, result.max_strike) == (20000.0, 20000.0)


def test_only_subscribed_codes_count_not_everything_in_the_cache():
    codes, rows = _chain([19400, 20000, 20600])
    wide = _option(18000)
    rows[wide["code"]] = wide  # in the cache, never subscribed

    [result] = chain_coverage(codes, rows, 20000.0)

    assert result.min_strike == 19400.0


def test_rows_that_say_nothing_about_the_chain_are_left_out():
    codes, rows = _chain([19400, 20000, 20600])
    skipped = [
        _option(18000, root="MXO", code="MXO18000C04"),  # another root
        _option(18001, type="future", code="TXFX"),  # not an option
        _option(18002, code="TXOZERO") | {"strike": "0"},  # no usable strike
        _option(18003, code="TXOBAD") | {"strike": "abc"},  # unparseable strike
        _option(18004, code="TXONODATE", delivery_date=""),  # no expiry
    ]
    for row in skipped:
        rows[row["code"]] = row

    result = chain_coverage([*rows, "UNKNOWNCODE"], rows, 20000.0)

    assert [(item.expiry, item.contracts, item.min_strike) for item in result] == [(_EXPIRY_ISO, 3, 19400.0)]


def test_a_code_listed_twice_is_counted_once():
    codes, rows = _chain([19400, 20000, 20600])

    [result] = chain_coverage([*codes, *codes], rows, 20000.0)

    assert result.contracts == 3


def test_an_empty_subscription_has_no_expiries():
    assert chain_coverage([], {}, 20000.0) == []


# ── the pool gauge ────────────────────────────────────────────────────────


def _future(price: float = 20000.0) -> dict[str, Any]:
    return {"code": "TXFB0", "type": "future", "root": "TXF", "delivery_date": "2030/03/20", "reference": price}


def _index(strikes: list[float], *, future: dict[str, Any] | None = None, **kwargs: Any) -> ContractIndex:
    rows = [_option(strike, **kwargs) for strike in strikes]
    return ContractIndex(contracts=[future if future is not None else _future(), *rows])


def _make_pool(tmp_path, strikes: list[float], **kwargs: Any) -> QuoteConnectionPool:
    sym_path = tmp_path / "symbols.yaml"
    symbols = [{"code": "TXFB0", "exchange": "TAIFEX", "group": 0}]
    symbols += [{"code": _option(strike, **kwargs)["code"], "exchange": "TAIFEX", "group": 0} for strike in strikes]
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
    pool: QuoteConnectionPool, index: ContractIndex, *, subscribed: set[str] | None = None
) -> list[ExpiryCoverage]:
    """Report with a facade that holds ``subscribed`` (default: everything the pool is configured with)."""
    held = {str(sym["code"]) for sym in pool._all_symbols} if subscribed is None else subscribed
    pool._clients = [SimpleNamespace(subscribed_codes=held)]
    with mock.patch.object(type(pool), "_contract_index", return_value=index):
        return pool.report_option_chain_coverage()


def test_the_pool_publishes_one_for_a_covered_chain(tmp_path):
    strikes = [19400, 19800, 20000, 20200, 20600]
    pool = _make_pool(tmp_path, strikes)

    result = _report(pool, _index(strikes))

    assert [item.covered for item in result] == [True]
    assert _gauge_values() == {_EXPIRY_ISO: 1.0}


def test_the_pool_publishes_zero_for_a_chain_that_stops_below_the_future(tmp_path):
    strikes = [19000, 19400, 19800, 20000]
    pool = _make_pool(tmp_path, strikes)

    _report(pool, _index(strikes))

    assert _gauge_values() == {_EXPIRY_ISO: 0.0}


def test_the_pool_publishes_minus_one_when_the_future_has_no_price(tmp_path):
    strikes = [19000, 21000]
    pool = _make_pool(tmp_path, strikes)

    _report(pool, ContractIndex(contracts=[_option(strike) for strike in strikes]))

    assert _gauge_values() == {_EXPIRY_ISO: -1.0}


def test_the_reference_is_the_future_not_an_option_premium(tmp_path):
    """An option row's ``reference`` is its premium (150 here); judging by it would call any chain covered."""
    strikes = [19000, 19400, 19800, 20000]
    pool = _make_pool(tmp_path, strikes)

    [result] = _report(pool, _index(strikes))

    assert result.reference == 20000.0
    assert result.covered is False


def test_only_what_the_pool_subscribes_to_is_judged(tmp_path):
    subscribed = [19800, 20000, 20200]
    pool = _make_pool(tmp_path, subscribed)

    # The cache lists a wide chain; the pool only holds the three centre strikes.
    [result] = _report(pool, _index([18000, 19800, 20000, 20200, 22000]))

    assert (result.min_strike, result.max_strike, result.covered) == (19800.0, 20200.0, False)


def test_an_expiry_that_rolled_off_stops_reporting(tmp_path):
    pool = _make_pool(tmp_path, [19400, 20600])
    _report(pool, _index([19400, 20600]))
    assert _gauge_values() == {_EXPIRY_ISO: 1.0}

    far = [19900, 20100]
    pool._all_symbols = [
        {"code": _option(strike, expiry=_FAR_EXPIRY)["code"], "exchange": "TAIFEX", "group": 0} for strike in far
    ]
    _report(pool, _index(far, expiry=_FAR_EXPIRY))

    assert _gauge_values() == {_FAR_EXPIRY_ISO: 0.0}


def test_an_unchanged_chain_logs_once_and_a_change_logs_again(tmp_path, monkeypatch):
    logger = mock.MagicMock()
    monkeypatch.setattr(qcp, "logger", logger)
    strikes = [19000, 19400, 19800, 20000]
    pool = _make_pool(tmp_path, strikes)
    logger.reset_mock()  # the constructor logs too

    for _ in range(3):
        _report(pool, _index(strikes))
    assert [call.args[0] for call in logger.warning.call_args_list] == ["option_chain_atm_coverage_missing"]

    # the chain now reaches the money: one INFO, no further warning
    wide = [19400, 20000, 20600]
    pool._all_symbols = [{"code": _option(strike)["code"], "exchange": "TAIFEX", "group": 0} for strike in wide]
    _report(pool, _index(wide))
    assert [call.args[0] for call in logger.info.call_args_list] == ["option_chain_atm_coverage"]
    assert len(logger.warning.call_args_list) == 1


def test_the_gauge_is_refreshed_even_when_the_log_is_suppressed(tmp_path):
    """The log is edge-triggered, the gauge is level-triggered: a scrape after a Prometheus restart must see it."""
    strikes = [19000, 20000]
    pool = _make_pool(tmp_path, strikes)
    _report(pool, _index(strikes))
    gauge = qcp._METRIC_ATM_COVERAGE
    assert gauge is not None
    gauge.clear()

    _report(pool, _index(strikes))

    assert _gauge_values() == {_EXPIRY_ISO: 0.0}


def test_a_missing_contract_cache_raises_to_the_caller_and_publishes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_CONTRACT_CACHE_PATH", str(tmp_path / "absent.json"))
    pool = _make_pool(tmp_path, [20000])

    with pytest.raises(FileNotFoundError):
        pool.report_option_chain_coverage()

    assert _gauge_values() == {}


# ── the refresh thread ────────────────────────────────────────────────────


class _StubEvent:
    """A ``threading.Event`` that lets the loop through ``cycles`` times and then stops it."""

    def __init__(self, is_set_script: list[bool]) -> None:
        self._script = list(is_set_script)

    def clear(self) -> None:
        pass

    def set(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> bool:
        return False

    def is_set(self) -> bool:
        return self._script.pop(0) if self._script else True


def _run_refresh_loop(pool: QuoteConnectionPool, is_set_script: list[bool]) -> None:
    pool._refresh_stop_event = _StubEvent(is_set_script)
    pool.start_options_refresh_thread(interval_s=1e-9)
    thread = pool._refresh_thread
    assert thread is not None
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_the_refresh_thread_reports_coverage_after_every_refresh(tmp_path):
    pool = _make_pool(tmp_path, [20000])
    refresh = mock.MagicMock(return_value=False)
    report = mock.MagicMock(return_value=[])

    with (
        mock.patch.object(type(pool), "refresh_options_symbols", refresh),
        mock.patch.object(type(pool), "report_option_chain_coverage", report),
    ):
        _run_refresh_loop(pool, [False, False, True])  # the initial refresh, then one timed refresh

    assert refresh.call_count == 2
    assert report.call_count == 2


def test_a_failing_report_does_not_end_the_refresh_thread(tmp_path, monkeypatch):
    logger = mock.MagicMock()
    monkeypatch.setattr(qcp, "logger", logger)
    pool = _make_pool(tmp_path, [20000])
    refresh = mock.MagicMock(return_value=False)
    report = mock.MagicMock(side_effect=RuntimeError("cache unreadable"))

    with (
        mock.patch.object(type(pool), "refresh_options_symbols", refresh),
        mock.patch.object(type(pool), "report_option_chain_coverage", report),
    ):
        _run_refresh_loop(pool, [False, False, True])

    assert refresh.call_count == 2, "the second refresh only happens if the first report's error was contained"
    assert [call.args[0] for call in logger.warning.call_args_list].count("option_chain_coverage_failed") == 2


def test_a_failing_refresh_still_reports_coverage(tmp_path):
    """The chain already subscribed is worth judging even when this hour's refresh could not run."""
    pool = _make_pool(tmp_path, [20000])
    report = mock.MagicMock(return_value=[])

    with (
        mock.patch.object(type(pool), "refresh_options_symbols", mock.MagicMock(side_effect=RuntimeError("down"))),
        mock.patch.object(type(pool), "report_option_chain_coverage", report),
    ):
        _run_refresh_loop(pool, [True])

    assert report.call_count == 1


def test_a_strike_the_broker_refused_is_not_counted_as_covered(tmp_path):
    """Configured up to 21000 but the subscribe stopped at the future (20000): the chain does not reach F+500."""
    configured = [19500, 20000, 20500, 21000]
    pool = _make_pool(tmp_path, configured)
    held = {pool._all_symbols[0]["code"]} | {_option(strike)["code"] for strike in configured if strike <= 20000}

    [result] = _report(pool, _index(configured), subscribed=held)

    assert (result.max_strike, result.covered) == (20000.0, False)
    assert _gauge_values() == {_EXPIRY_ISO: 0.0}
