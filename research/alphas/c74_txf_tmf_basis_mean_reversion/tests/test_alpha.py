"""Unit tests for C74 TXF-TMF basis mean-reversion.

Patterns per .agent/skills/hft-test-hft/SKILL.md:
  - Scaled-int price assertions (CK scale = 1_000_000)
  - Monotonic time via time.monotonic_ns()

DA T2 critical conditions verified:
  - Hedge ratio 20 (physics-fixed)
  - Basis arithmetic (dollar-neutral)
  - Stale-quote filter |basis|>50 pt with counter
  - Rolling mu/sigma over configurable window
  - Entry trigger at 2-sigma (both directions)
  - Exit on reversion to mu (both directions)
  - Exit on timeout (30-min default)
  - Stop-loss at 4-sigma (TAKER cross)
  - Exchange-ts alignment (cross-instrument)
  - Mutual exclusion documentation
  - AlphaProtocol conformance
"""

from __future__ import annotations

import time

import pytest

from research.alphas.c74_txf_tmf_basis_mean_reversion.impl import (
    _HEDGE_RATIO_TMF_PER_TXF,
    _STALE_QUOTE_FILTER_BASIS_PT,
    _TMF_INST_RT_COST_PTS,
    _TMF_POINT_VALUE_NTD,
    _TXF_INST_RT_COST_PTS,
    _TXF_POINT_VALUE_NTD,
    C74Alpha,
    C74Params,
    RollingBasisStats,
    TxfTmfBasisMeanReversion,
)
from research.backtest.maker_engine import (
    Hold,
    PostQuote,
    TickData,
)
from research.registry.schemas import AlphaProtocol

_SCALE = 1_000_000


def _bidask(
    bid_pts: int | float,
    ask_pts: int | float,
    bid_qty: int = 10,
    ask_qty: int = 10,
    ts_ns: int | None = None,
) -> TickData:
    return TickData(
        exch_ts=ts_ns if ts_ns is not None else time.monotonic_ns(),
        bid_price=int(bid_pts * _SCALE),
        ask_price=int(ask_pts * _SCALE),
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        trade_price=0,
        trade_volume=0,
        is_trade=False,
        scale=_SCALE,
    )


def _trade(
    price_pts: int | float, volume: int = 1, ts_ns: int | None = None
) -> TickData:
    return TickData(
        exch_ts=ts_ns if ts_ns is not None else time.monotonic_ns(),
        bid_price=0,
        ask_price=0,
        bid_qty=0,
        ask_qty=0,
        trade_price=int(price_pts * _SCALE),
        trade_volume=volume,
        is_trade=True,
        scale=_SCALE,
    )


# ----------------------------------------------------------------------------
# Physics constants
# ----------------------------------------------------------------------------


def test_txf_point_value_200_ntd() -> None:
    assert _TXF_POINT_VALUE_NTD == 200


def test_tmf_point_value_10_ntd() -> None:
    assert _TMF_POINT_VALUE_NTD == 10


def test_hedge_ratio_is_20_dollar_neutral() -> None:
    """1:20 from TXF 200 NTD/pt / TMF 10 NTD/pt = 20. Fixed by physics."""
    assert _HEDGE_RATIO_TMF_PER_TXF == 20
    assert _HEDGE_RATIO_TMF_PER_TXF == _TXF_POINT_VALUE_NTD // _TMF_POINT_VALUE_NTD


def test_inst_rt_costs() -> None:
    assert _TXF_INST_RT_COST_PTS == 1.5
    assert _TMF_INST_RT_COST_PTS == 1.5


def test_stale_filter_default_50pt() -> None:
    assert _STALE_QUOTE_FILTER_BASIS_PT == 50


# ----------------------------------------------------------------------------
# RollingBasisStats
# ----------------------------------------------------------------------------


def test_rolling_stats_empty() -> None:
    s = RollingBasisStats(window_ns=60_000_000_000)
    assert s.n() == 0
    assert s.mean() == 0.0
    assert s.stdev() == 0.0


def test_rolling_stats_single_sample() -> None:
    s = RollingBasisStats(window_ns=60_000_000_000)
    s.push(1_000_000_000, 5.0)
    assert s.n() == 1
    assert s.mean() == 5.0
    assert s.stdev() == 0.0


def test_rolling_stats_multi_sample_mean() -> None:
    s = RollingBasisStats(window_ns=60_000_000_000)
    for i, v in enumerate([1.0, 2.0, 3.0, 4.0, 5.0]):
        s.push(i * 1_000_000_000, v)
    assert s.n() == 5
    assert s.mean() == 3.0


def test_rolling_stats_stdev() -> None:
    s = RollingBasisStats(window_ns=60_000_000_000)
    for i, v in enumerate([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]):
        s.push(i * 1_000_000_000, v)
    # Sample stdev (n-1 divisor) of this sequence is 2.138
    assert abs(s.stdev() - 2.138) < 0.01


def test_rolling_stats_eviction() -> None:
    s = RollingBasisStats(window_ns=10_000_000_000)
    s.push(0, 1.0)
    s.push(5_000_000_000, 2.0)
    assert s.n() == 2
    # t=20_000_000_000 evicts both earlier samples (t=0 expires before t=10B)
    s.push(20_000_000_000, 3.0)
    assert s.n() == 1
    assert s.mean() == 3.0


def test_rolling_stats_reset() -> None:
    s = RollingBasisStats(window_ns=60_000_000_000)
    s.push(1, 1.0)
    s.push(2, 2.0)
    s.reset()
    assert s.n() == 0


# ----------------------------------------------------------------------------
# Basis computation
# ----------------------------------------------------------------------------


def test_basis_arithmetic_dollar_neutral() -> None:
    strat = TxfTmfBasisMeanReversion()
    # TXF mid 17500, TMF mid 875 => basis = 17500 - 20*875 = 0
    strat.update_mid("TXFD6", _bidask(17499, 17501, ts_ns=1))
    strat.update_mid("TMFD6", _bidask(17499.5, 17500.5, ts_ns=2))
    # Basis should have been stored inside strat; inspect via rolling stats
    assert abs(strat.rolling_mean - 0.0) < 0.01


def test_basis_positive_extreme() -> None:
    """TXF mid 17500, TMF mid 870 => basis = 17500 - 20*870 = 100 pt (extreme)."""
    strat = TxfTmfBasisMeanReversion()
    strat.update_mid("TXFD6", _bidask(17499, 17501, ts_ns=1))
    # basis = 17500 - 20*870 = 100 (exceeds stale filter of 50 -> hit logged)
    strat.update_mid("TMFD6", _bidask(869.5, 870.5, ts_ns=2))
    assert strat.stale_filter_hits >= 1


def test_basis_negative_extreme_within_filter() -> None:
    """Moderate negative basis within filter."""
    strat = TxfTmfBasisMeanReversion()
    strat.update_mid("TXFD6", _bidask(17499, 17501, ts_ns=1))
    # basis = 17500 - 20*876 = -20 (within 50-pt filter)
    strat.update_mid("TMFD6", _bidask(17500.5, 17501.5, ts_ns=2))
    assert strat.stale_filter_hits == 0


# ----------------------------------------------------------------------------
# Stale-quote filter (DA T2 flag #3)
# ----------------------------------------------------------------------------


def test_stale_filter_counter_advances() -> None:
    """|basis| > 50 pt increments stale_filter_hits."""
    strat = TxfTmfBasisMeanReversion()
    strat.update_mid("TXFD6", _bidask(17999, 18001, ts_ns=1))  # mid 18000
    # Choose TMF mid 800 => basis = 18000 - 16000 = 2000 (extreme)
    strat.update_mid("TMFD6", _bidask(799.5, 800.5, ts_ns=2))
    assert strat.stale_filter_hits == 1


def test_stale_filter_does_not_update_stats_when_tripped() -> None:
    """When stale filter fires, rolling stats must NOT get the polluted sample."""
    strat = TxfTmfBasisMeanReversion()
    strat.update_mid("TXFD6", _bidask(17999, 18001, ts_ns=1))
    strat.update_mid("TMFD6", _bidask(799.5, 800.5, ts_ns=2))
    assert strat.rolling_n == 0


def test_stale_filter_custom_threshold() -> None:
    """Param stale_basis_filter_pt is configurable."""
    p = C74Params(stale_basis_filter_pt=5)
    strat = TxfTmfBasisMeanReversion(params=p)
    strat.update_mid("TXFD6", _bidask(17499, 17501, ts_ns=1))
    # basis = 17500 - 17490 = 10 (above 5-pt custom filter)
    strat.update_mid("TMFD6", _bidask(17489.5, 17490.5, ts_ns=2))
    assert strat.stale_filter_hits == 1


# ----------------------------------------------------------------------------
# Warm-up: no entry before min_samples
# ----------------------------------------------------------------------------


def test_no_entry_before_warmup_samples() -> None:
    p = C74Params(min_samples_for_entry=5, entry_sigma=0.5)
    strat = TxfTmfBasisMeanReversion(params=p)
    # Only 3 samples (insufficient for warm-up)
    for i in range(3):
        strat.update_mid("TXFD6", _bidask(17499, 17501, ts_ns=i * 1_000_000_000))
        strat.update_mid("TMFD6", _bidask(17499.5, 17500.5, ts_ns=i * 1_000_000_000 + 1))
    assert strat.entries_posted == 0


# ----------------------------------------------------------------------------
# Entry trigger (short basis on positive extreme; long basis on negative)
# ----------------------------------------------------------------------------


def _warm_up_and_get_strat(
    n_warmup: int = 20,
    entry_sigma: float = 2.0,
    stop_sigma: float = 4.0,
    timeout_seconds: int = 99999,
) -> TxfTmfBasisMeanReversion:
    """Build up warmup samples with small, bounded basis variation.

    After warm-up, basis ~ 0 with small sigma (~0.5 pt). Caller then
    pushes an extreme deviation that exceeds entry_sigma*sigma.
    """
    p = C74Params(
        min_samples_for_entry=n_warmup,
        entry_sigma=entry_sigma,
        stop_sigma=stop_sigma,
        timeout_seconds=timeout_seconds,
    )
    strat = TxfTmfBasisMeanReversion(params=p)
    # Alternating small noise to yield non-zero sigma without entries.
    for i in range(n_warmup):
        ts = i * 1_000_000_000
        offset = 0.5 if i % 2 == 0 else -0.5
        strat.update_mid(
            "TXFD6",
            _bidask(17500 + offset, 17502 + offset, ts_ns=ts),
        )
        strat.update_mid("TMFD6", _bidask(17499.5, 17500.5, ts_ns=ts + 1))
    assert strat.entries_posted == 0, (
        "Warmup alone must not trigger entries"
    )
    return strat


def test_entry_short_basis_at_positive_extreme() -> None:
    """TXF mid >> 20*TMF triggers sell-TXF + buy-TMF (on the TXF update
    alone — entry fires on the update that creates the extreme)."""
    strat = _warm_up_and_get_strat()
    ts = 20_000_000_000
    # TXF push creates extreme basis of ~+20 pt; entry fires on this tick
    actions = strat.update_mid("TXFD6", _bidask(17520, 17522, ts_ns=ts))
    assert strat.entries_posted == 1
    # Entry side may have exited via subsequent updates; check it WAS short.
    # We assert on posts emitted at entry-time, not the post-entry state.
    posts = [a for a in actions if isinstance(a, PostQuote)]
    sides = {a.side for a in posts}
    assert sides == {"sell_txf", "buy_tmf"}


def test_entry_long_basis_at_negative_extreme() -> None:
    strat = _warm_up_and_get_strat()
    ts = 20_000_000_000
    # TXF push creates extreme basis of ~-20 pt
    actions = strat.update_mid("TXFD6", _bidask(17480, 17482, ts_ns=ts))
    assert strat.entries_posted == 1
    posts = [a for a in actions if isinstance(a, PostQuote)]
    sides = {a.side for a in posts}
    assert sides == {"buy_txf", "sell_tmf"}


def test_entry_returns_both_legs_maker() -> None:
    """Both legs post MAKER on entry (no '_taker' suffix)."""
    strat = _warm_up_and_get_strat()
    ts = 20_000_000_000
    actions = strat.update_mid("TXFD6", _bidask(17520, 17522, ts_ns=ts))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2
    assert all("_taker" not in a.side for a in posts)


def test_entry_hedge_qty_is_20_to_1() -> None:
    """TMF leg quantity = 20 * TXF leg quantity (dollar-neutral)."""
    strat = _warm_up_and_get_strat()
    ts = 20_000_000_000
    actions = strat.update_mid("TXFD6", _bidask(17520, 17522, ts_ns=ts))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    by_side = {a.side: a for a in posts}
    assert by_side["sell_txf"].qty == 1
    assert by_side["buy_tmf"].qty == 20


def test_no_entry_within_threshold() -> None:
    """Basis within 1-sigma of mu: no entry."""
    strat = _warm_up_and_get_strat()
    ts = 20_000_000_000
    # Small deviation
    strat.update_mid("TXFD6", _bidask(17500, 17502, ts_ns=ts))
    strat.update_mid("TMFD6", _bidask(17499.5, 17500.5, ts_ns=ts + 1))
    assert strat.entries_posted == 0
    assert strat.open_trip is None


# ----------------------------------------------------------------------------
# Exit logic: reversion, timeout, stop-loss
# ----------------------------------------------------------------------------


def test_exit_on_reversion_to_mu() -> None:
    """After entry at +extreme, basis crossing mu triggers reversion exit.

    Uses direct state injection to bypass rolling-stats interaction:
    inject an open short_basis trip with fixed entry_mu, then push a basis
    observation below mu to trigger reversion."""
    from research.alphas.c74_txf_tmf_basis_mean_reversion.impl import _OpenTrip
    strat = TxfTmfBasisMeanReversion()
    # Manually bootstrap state: stats populated around mu=0
    for i in range(30):
        offset = 0.5 if i % 2 == 0 else -0.5
        strat._stats.push(i * 1_000_000_000, offset)
    strat._last_txf_bid = 17499 * _SCALE
    strat._last_txf_ask = 17501 * _SCALE
    strat._last_tmf_bid = int(17499.5 * _SCALE)
    strat._last_tmf_ask = int(17500.5 * _SCALE)
    strat._last_txf_mid_pts = 17500.0
    strat._last_tmf_mid_pts = 17500.0
    strat._open_trip = _OpenTrip(
        side="short_basis",
        entry_ts_ns=20_000_000_000,
        entry_basis_pts=20.0,
        entry_mu_pts=0.0,
        entry_sigma_pts=0.5,
        txf_bid=17520 * _SCALE,
        txf_ask=17522 * _SCALE,
        tmf_bid=int(17499.5 * _SCALE),
        tmf_ask=int(17500.5 * _SCALE),
    )
    # Now push a TXF tick that makes basis ≈ 0 (reverted to mu).
    ts2 = 25_000_000_000
    strat.update_mid("TXFD6", _bidask(17499, 17501, ts_ns=ts2))
    strat.update_mid("TMFD6", _bidask(17499.5, 17500.5, ts_ns=ts2 + 1))
    assert strat.open_trip is None
    assert strat.exits_reversion == 1


def test_exit_on_timeout() -> None:
    """Open trip elapsed > timeout_seconds triggers timeout exit.

    Use wide stop_sigma (9999) so stop-loss doesn't pre-empt timeout; the
    only exit condition remaining at high basis is the timeout itself."""
    from research.alphas.c74_txf_tmf_basis_mean_reversion.impl import _OpenTrip

    p = C74Params(timeout_seconds=5, stop_sigma=9999.0)
    strat = TxfTmfBasisMeanReversion(params=p)
    for i in range(30):
        offset = 0.5 if i % 2 == 0 else -0.5
        strat._stats.push(i * 1_000_000_000, offset)
    strat._last_txf_mid_pts = 17520.0
    strat._last_tmf_mid_pts = 17500.0
    strat._last_txf_bid = 17519 * _SCALE
    strat._last_txf_ask = 17521 * _SCALE
    strat._last_tmf_bid = int(17499.5 * _SCALE)
    strat._last_tmf_ask = int(17500.5 * _SCALE)
    strat._open_trip = _OpenTrip(
        side="short_basis",
        entry_ts_ns=20_000_000_000,
        entry_basis_pts=20.0,
        entry_mu_pts=0.0,
        entry_sigma_pts=0.5,
        txf_bid=17520 * _SCALE,
        txf_ask=17522 * _SCALE,
        tmf_bid=int(17499.5 * _SCALE),
        tmf_ask=int(17500.5 * _SCALE),
    )
    # 6 seconds after entry, basis still high (no reversion), stop disabled
    ts2 = 20_000_000_000 + 6_000_000_000
    strat.update_mid("TXFD6", _bidask(17520, 17522, ts_ns=ts2))
    strat.update_mid("TMFD6", _bidask(17499.5, 17500.5, ts_ns=ts2 + 1))
    assert strat.open_trip is None
    assert strat.exits_timeout == 1


def test_exit_on_stop_loss_taker() -> None:
    """Basis deviation > stop_sigma*sigma_entry -> TAKER cross both legs."""
    from research.alphas.c74_txf_tmf_basis_mean_reversion.impl import _OpenTrip

    p = C74Params(stop_sigma=2.0, timeout_seconds=99999)
    strat = TxfTmfBasisMeanReversion(params=p)
    for i in range(30):
        offset = 0.5 if i % 2 == 0 else -0.5
        strat._stats.push(i * 1_000_000_000, offset)
    strat._last_txf_bid = 17519 * _SCALE
    strat._last_txf_ask = 17521 * _SCALE
    strat._last_tmf_bid = int(17499.5 * _SCALE)
    strat._last_tmf_ask = int(17500.5 * _SCALE)
    strat._last_txf_mid_pts = 17520.0
    strat._last_tmf_mid_pts = 17500.0
    # Entry at basis=20 with sigma=0.5 and stop_sigma=2 => stop at |dev|>1
    strat._open_trip = _OpenTrip(
        side="short_basis",
        entry_ts_ns=20_000_000_000,
        entry_basis_pts=20.0,
        entry_mu_pts=0.0,
        entry_sigma_pts=0.5,
        txf_bid=17520 * _SCALE,
        txf_ask=17522 * _SCALE,
        tmf_bid=int(17499.5 * _SCALE),
        tmf_ask=int(17500.5 * _SCALE),
    )
    # Push basis to ~25 => |basis - 0| = 25 > 2*0.5 = 1 -> stop fires.
    ts2 = 21_000_000_000
    # TXF push is what triggers the exit (basis becomes extreme)
    actions = strat.update_mid("TXFD6", _bidask(17524, 17526, ts_ns=ts2))
    assert strat.open_trip is None
    assert strat.exits_stop_loss == 1
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert any("_taker" in a.side for a in posts)


# ----------------------------------------------------------------------------
# Closed-trip record keeping (for PnL distribution, DA flag #6)
# ----------------------------------------------------------------------------


def test_closed_trips_recorded_on_reversion() -> None:
    """Closed-trip record contains exit_reason=reversion and
    taker_close=False when the trip closes via mean-reversion."""
    from research.alphas.c74_txf_tmf_basis_mean_reversion.impl import _OpenTrip

    strat = TxfTmfBasisMeanReversion()
    for i in range(30):
        offset = 0.5 if i % 2 == 0 else -0.5
        strat._stats.push(i * 1_000_000_000, offset)
    strat._last_txf_mid_pts = 17500.0
    strat._last_tmf_mid_pts = 17500.0
    strat._last_txf_bid = 17499 * _SCALE
    strat._last_txf_ask = 17501 * _SCALE
    strat._last_tmf_bid = int(17499.5 * _SCALE)
    strat._last_tmf_ask = int(17500.5 * _SCALE)
    strat._open_trip = _OpenTrip(
        side="short_basis",
        entry_ts_ns=20_000_000_000,
        entry_basis_pts=20.0,
        entry_mu_pts=0.0,
        entry_sigma_pts=0.5,
        txf_bid=17520 * _SCALE,
        txf_ask=17522 * _SCALE,
        tmf_bid=int(17499.5 * _SCALE),
        tmf_ask=int(17500.5 * _SCALE),
    )
    # Revert basis to ~0
    ts2 = 25_000_000_000
    strat.update_mid("TXFD6", _bidask(17499, 17501, ts_ns=ts2))
    strat.update_mid("TMFD6", _bidask(17499.5, 17500.5, ts_ns=ts2 + 1))
    trips = strat.closed_trips
    # There is exactly one pre-injected trip; the test checks it was closed
    # with the correct attributes on that specific trip.
    # Find the injected trip (entry_ts=20B)
    injected = [t for t in trips if t["entry_ts_ns"] == 20_000_000_000]
    assert len(injected) == 1
    assert injected[0]["side"] == "short_basis"
    assert injected[0]["exit_reason"] == "reversion"
    assert injected[0]["taker_close"] is False


def test_closed_trips_record_stop_loss_as_taker() -> None:
    """Closed-trip record contains exit_reason=stop_loss and taker_close=True."""
    from research.alphas.c74_txf_tmf_basis_mean_reversion.impl import _OpenTrip

    p = C74Params(stop_sigma=2.0, timeout_seconds=99999)
    strat = TxfTmfBasisMeanReversion(params=p)
    for i in range(30):
        offset = 0.5 if i % 2 == 0 else -0.5
        strat._stats.push(i * 1_000_000_000, offset)
    strat._last_txf_bid = 17519 * _SCALE
    strat._last_txf_ask = 17521 * _SCALE
    strat._last_tmf_bid = int(17499.5 * _SCALE)
    strat._last_tmf_ask = int(17500.5 * _SCALE)
    strat._last_txf_mid_pts = 17520.0
    strat._last_tmf_mid_pts = 17500.0
    strat._open_trip = _OpenTrip(
        side="short_basis",
        entry_ts_ns=20_000_000_000,
        entry_basis_pts=20.0,
        entry_mu_pts=0.0,
        entry_sigma_pts=0.5,
        txf_bid=17520 * _SCALE,
        txf_ask=17522 * _SCALE,
        tmf_bid=int(17499.5 * _SCALE),
        tmf_ask=int(17500.5 * _SCALE),
    )
    ts2 = 21_000_000_000
    strat.update_mid("TXFD6", _bidask(17524, 17526, ts_ns=ts2))
    strat.update_mid("TMFD6", _bidask(17499.5, 17500.5, ts_ns=ts2 + 1))
    trips = strat.closed_trips
    injected = [t for t in trips if t["entry_ts_ns"] == 20_000_000_000]
    assert len(injected) == 1
    assert injected[0]["exit_reason"] == "stop_loss"
    assert injected[0]["taker_close"] is True


# ----------------------------------------------------------------------------
# Symbol routing
# ----------------------------------------------------------------------------


def test_unknown_symbol_returns_hold() -> None:
    strat = TxfTmfBasisMeanReversion()
    actions = strat.update_mid("UNKNOWN", _bidask(17500, 17502))
    assert actions == [Hold()]


def test_trade_event_returns_hold() -> None:
    strat = TxfTmfBasisMeanReversion()
    actions = strat.update_mid("TXFD6", _trade(17501))
    assert actions == [Hold()]


def test_single_leg_no_basis_computation() -> None:
    """Only TXF update (no TMF) should not trigger basis-based logic."""
    strat = TxfTmfBasisMeanReversion()
    strat.update_mid("TXFD6", _bidask(17500, 17502))
    # Basis requires BOTH legs; tick_count incremented but no stale hit.
    assert strat.tick_count == 1
    assert strat.stale_filter_hits == 0
    assert strat.rolling_n == 0


# ----------------------------------------------------------------------------
# Custom window / sigma params (T5 sweep ranges)
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("window_sec", [300, 1800, 3600])
def test_window_seconds_sweep(window_sec: int) -> None:
    p = C74Params(window_seconds=window_sec)
    strat = TxfTmfBasisMeanReversion(params=p)
    assert strat.params.window_seconds == window_sec


@pytest.mark.parametrize("entry_sigma", [1.5, 2.0, 2.5])
def test_entry_sigma_sweep(entry_sigma: float) -> None:
    p = C74Params(entry_sigma=entry_sigma)
    strat = TxfTmfBasisMeanReversion(params=p)
    assert strat.params.entry_sigma == entry_sigma


# ----------------------------------------------------------------------------
# Reset / gap resilience
# ----------------------------------------------------------------------------


def test_reset_clears_all_state() -> None:
    """reset() clears rolling stats, open trip, counters."""
    from research.alphas.c74_txf_tmf_basis_mean_reversion.impl import _OpenTrip

    strat = _warm_up_and_get_strat()
    # Inject an open trip directly
    strat._open_trip = _OpenTrip(
        side="short_basis",
        entry_ts_ns=20_000_000_000,
        entry_basis_pts=20.0,
        entry_mu_pts=0.0,
        entry_sigma_pts=0.5,
        txf_bid=17520 * _SCALE,
        txf_ask=17522 * _SCALE,
        tmf_bid=int(17499.5 * _SCALE),
        tmf_ask=int(17500.5 * _SCALE),
    )
    assert strat.open_trip is not None
    strat.reset()
    assert strat.open_trip is None
    assert strat.entries_posted == 0
    assert strat.rolling_n == 0
    assert strat.stale_filter_hits == 0


def test_on_gap_resets_stats_but_not_trip() -> None:
    """on_gap clears rolling stats but leaves open trip for external flatten."""
    from research.alphas.c74_txf_tmf_basis_mean_reversion.impl import _OpenTrip

    strat = _warm_up_and_get_strat()
    strat._open_trip = _OpenTrip(
        side="short_basis",
        entry_ts_ns=20_000_000_000,
        entry_basis_pts=20.0,
        entry_mu_pts=0.0,
        entry_sigma_pts=0.5,
        txf_bid=17520 * _SCALE,
        txf_ask=17522 * _SCALE,
        tmf_bid=int(17499.5 * _SCALE),
        tmf_ask=int(17500.5 * _SCALE),
    )
    assert strat.open_trip is not None
    strat.on_gap()
    assert strat.open_trip is not None  # trip preserved
    assert strat.rolling_n == 0          # stats reset


# ----------------------------------------------------------------------------
# Timestamp alignment (DA flag #5)
# ----------------------------------------------------------------------------


def test_exch_ts_used_for_timing_not_wall_clock() -> None:
    """exch_ts supplied by tick is what the strategy records."""
    strat = TxfTmfBasisMeanReversion()
    strat.update_mid("TXFD6", _bidask(17499, 17501, ts_ns=12345))
    # update_mid records exch_ts inside strat._last_ts_ns
    assert strat._last_ts_ns == 12345


# ----------------------------------------------------------------------------
# AlphaProtocol conformance
# ----------------------------------------------------------------------------


def test_c74_alpha_conforms_to_alpha_protocol() -> None:
    alpha = C74Alpha()
    assert isinstance(alpha, AlphaProtocol)
    assert alpha.manifest.alpha_id == "c74_txf_tmf_basis_mean_reversion"
    sig = alpha.update(foo=1)
    assert isinstance(sig, float)
    assert alpha.reset() is None


def test_c74_manifest_declares_latency_profile() -> None:
    alpha = C74Alpha()
    assert alpha.manifest.latency_profile


def test_c74_manifest_documents_cross_instrument() -> None:
    alpha = C74Alpha()
    # Cross-instrument => instrument string has both tickers
    assert "TXFD6" in alpha.manifest.instrument
    assert "TMFD6" in alpha.manifest.instrument


def test_c74_hypothesis_cites_dollar_neutral() -> None:
    alpha = C74Alpha()
    h = alpha.manifest.hypothesis.lower()
    assert "dollar-neutral" in h or "20*mid_tmf" in h.lower()


def test_c74_hypothesis_cites_r7_c66_distinction() -> None:
    """Must document distinction from R7 C66 (hedge-cost dominance)."""
    alpha = C74Alpha()
    h = alpha.manifest.hypothesis
    assert "C66" in h or "R7" in h


def test_c74_reset_clears_state() -> None:
    alpha = C74Alpha()
    alpha.strategy.update_mid("TXFD6", _bidask(17500, 17502, ts_ns=1))
    alpha.strategy.update_mid("TMFD6", _bidask(17499.5, 17500.5, ts_ns=2))
    assert alpha.strategy.tick_count > 0
    alpha.reset()
    assert alpha.strategy.tick_count == 0


# ----------------------------------------------------------------------------
# Mutual-exclusion documentation (DA flag #9)
# ----------------------------------------------------------------------------


def test_mutual_exclusion_documented_in_manifest_promote_prereqs() -> None:
    """Manifest yaml must document mutual exclusion with C63 (verified at
    yaml level; this test validates it is loaded/readable at the path)."""
    from pathlib import Path

    import yaml

    path = Path(
        "/home/charlie/hft_platform/research/alphas/"
        "c74_txf_tmf_basis_mean_reversion/manifest.yaml"
    )
    assert path.exists()
    doc = yaml.safe_load(path.read_text())
    prereqs = doc.get("promote_prerequisites", {})
    assert prereqs.get("mutually_exclusive_with_c63_on_txfd6") is True


def test_manifest_documents_all_10_da_mandatory_flags() -> None:
    from pathlib import Path

    import yaml

    path = Path(
        "/home/charlie/hft_platform/research/alphas/"
        "c74_txf_tmf_basis_mean_reversion/manifest.yaml"
    )
    doc = yaml.safe_load(path.read_text())
    flags = doc.get("da_t5_mandatory_flags", [])
    assert len(flags) == 10
    flag_ids = {f["id"] for f in flags}
    expected = {
        "1_broker_confirmation",
        "2_adaptive_rolling_sigma",
        "3_stale_quote_filter",
        "4_maker_both_legs",
        "5_exch_ts_alignment",
        "6_per_trip_pnl_distribution",
        "7_no_double_count",
        "8_session_sigma_quartile_grid",
        "9_mutual_exclusion_c63",
        "10_fanelli_precedent",
    }
    assert flag_ids == expected
