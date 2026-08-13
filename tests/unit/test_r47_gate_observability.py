"""The strategy's periodic diagnostic must survive the gates that block it.

Measured on THESHOW across 2026-08-10..12: r47_maker emitted 55 ``r47_stats``
lines in 24 hours and **every one of them** printed ``spread_pts: 5``, while
the true mean spread on the bound contract was 2.96 points and the engine's own
``spr_blk`` counter stood at 1,326,756 — 94.19% of ticks blocked.

The two facts fit together: ``_log_stats`` was called from inside
``_generate_quotes``, which only runs once all three placement gates have
passed. So the sample was drawn exclusively from the ticks that were *not*
blocked. The one diagnostic that could have explained a two-day quoting freeze
was emitted only while the strategy was not frozen.

The same period had no metric at all for ``_pending_buy``/``_pending_sell`` —
the counters that gate ``can_buy``/``can_sell`` and that a lost broker callback
latches high permanently. The state at the centre of the outage was
unobservable, so "latched off" and "nothing to quote on" looked identical from
outside.

These tests pin both: the diagnostic fires on every path with the blocking gate
named, and pending quantity reaches Prometheus.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.events import LOBStatsEvent

_PRICE_SCALE = 10000


def _lob_stats(symbol: str = "TMFE6", spread_scaled: int = 5 * _PRICE_SCALE) -> LOBStatsEvent:
    return LOBStatsEvent(
        symbol=symbol,
        ts=0,
        imbalance=0.0,
        best_bid=3728_0000,
        best_ask=3733_0000,
        bid_depth=10,
        ask_depth=10,
        mid_price_x2=7462_0000,
        spread_scaled=spread_scaled,
    )


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.positions = {}
    ctx.strategy_id = "r47_test"
    ctx.place_order = MagicMock(side_effect=lambda **kw: MagicMock(**kw))
    return ctx


@pytest.fixture()
def r47():
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    strat = R47MakerStrategy(
        strategy_id="r47_test",
        pe_danger_threshold=0.0,
        pe_window=100,
        queue_cancel_threshold=1.0,
        mfg_skew_z_threshold=100.0,
        spread_threshold_pts=5,
        toxicity_max=9999,
        qi_skew_threshold=0.10,
        qi_widen_ticks=1,
        max_pos=1,
    )
    strat.symbols = {"TMFE6"}
    return strat


@pytest.fixture()
def emit(module_log_sink):
    """Run one tick and return the ``r47_stats`` lines it produced.

    structlog renders straight to stdout here rather than through stdlib
    logging, so caplog never sees these. ``capture_logs`` does not work either —
    see ``module_log_sink`` in conftest for why it goes blind mid-session.
    """
    from hft_platform.strategies import r47_maker

    entries = module_log_sink(r47_maker)

    def _emit(r47, event) -> list[dict]:
        del entries[:]
        r47.handle_event(_ctx(), event)
        return [entry for entry in entries if entry.get("event") == "r47_stats"]

    return _emit


# --------------------------------------------------------------------------- #
# The diagnostic must outlive the gate                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_stats_are_logged_on_the_first_tick_even_when_the_spread_gate_blocks(r47, emit) -> None:
    """The regression: a sub-threshold spread used to produce no diagnostic at
    all, so the log could never show why quoting had stopped."""
    lines = emit(r47, _lob_stats(spread_scaled=2 * _PRICE_SCALE))

    assert len(lines) == 1
    assert lines[0]["blocked_by"] == "spread"


@pytest.mark.unit
def test_the_logged_spread_is_the_blocked_one_not_a_survivor(r47, emit) -> None:
    """55 survivor lines all printed spread_pts:5 while the mean was 2.96. The
    diagnostic has to report the tick it actually saw."""
    assert emit(r47, _lob_stats(spread_scaled=2 * _PRICE_SCALE))[0]["spread_pts"] == 2


@pytest.mark.unit
def test_stats_name_no_gate_when_quoting_proceeds(r47, emit) -> None:
    assert emit(r47, _lob_stats(spread_scaled=5 * _PRICE_SCALE))[0]["blocked_by"] is None


@pytest.mark.unit
def test_stats_report_pending_so_a_latched_strategy_is_visible_in_the_log(r47, emit) -> None:
    """``pending_buy``/``pending_sell`` are what gate further quoting. With them
    in the line, "latched off" is distinguishable from "nothing to quote on"."""
    line = emit(r47, _lob_stats(spread_scaled=5 * _PRICE_SCALE))[0]

    assert line["pending_buy"] == 1
    assert line["pending_sell"] == 1


@pytest.mark.unit
def test_the_spread_gate_still_suppresses_quoting(r47) -> None:
    """The gate chain was restructured from early returns to a recorded reason.
    Suppression behaviour must be unchanged."""
    assert r47.handle_event(_ctx(), _lob_stats(spread_scaled=2 * _PRICE_SCALE)) == []


@pytest.mark.unit
def test_a_passing_tick_still_produces_quotes(r47) -> None:
    assert r47.handle_event(_ctx(), _lob_stats(spread_scaled=5 * _PRICE_SCALE)) != []


# --------------------------------------------------------------------------- #
# The metrics                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_pending_quantity_reaches_prometheus(r47) -> None:
    """The core state of the 2026-08-10 freeze had no metric at all."""
    gauge = r47._metric_pending_qty
    assert gauge is not None, "strategy_pending_qty must exist in the registry"

    r47._pending_buy["TMFE6"] = 3
    r47._publish_gate_and_pending_metrics("TMFE6")

    assert gauge.labels(strategy="r47_test", symbol="TMFE6", side="BUY")._value.get() == 3


@pytest.mark.unit
def test_gate_blocks_reach_prometheus_as_an_exact_total(r47) -> None:
    """Published as a delta at the log interval rather than per tick — the
    spread gate fires on ~94% of ticks and a per-tick labels() lookup on that
    path is avoidable hot-path work. The published total must still be exact."""
    counter = r47._metric_gate_blocked
    assert counter is not None
    child = counter.labels(strategy="r47_test", gate="spread")
    before = child._value.get()

    r47._spread_blocked = 1000
    r47._publish_gate_and_pending_metrics("TMFE6")
    r47._spread_blocked = 1500
    r47._publish_gate_and_pending_metrics("TMFE6")

    assert child._value.get() == before + 1500


@pytest.mark.unit
def test_republishing_an_unchanged_gate_total_does_not_double_count(r47) -> None:
    counter = r47._metric_gate_blocked
    child = counter.labels(strategy="r47_test", gate="spread")

    r47._spread_blocked = 700
    r47._publish_gate_and_pending_metrics("TMFE6")
    after_first = child._value.get()
    r47._publish_gate_and_pending_metrics("TMFE6")

    assert child._value.get() == after_first
