"""TakerSim (research v2 C2b): fills come from the exchange model, not from mid-to-mid."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("hftbacktest")

from research.backtest.cost_models import TAIFEXCost  # noqa: E402
from research.backtest.hft_native_runner import ensure_hftbt_npz  # noqa: E402
from research.backtest.taker_sim import (  # noqa: E402
    METHOD_ID,
    TakerSim,
    TakerSimError,
    _TakerOrderBridge,
    daily_sharpe,
    simulate_files,
    split_trading_days,
)

_DTYPE = np.dtype(
    [
        ("bid_px", "f8"),
        ("ask_px", "f8"),
        ("bid_qty", "f8"),
        ("ask_qty", "f8"),
        ("mid_price", "f8"),
        ("spread_bps", "f8"),
        ("volume", "f8"),
        ("local_ts", "i8"),
    ]
)
_SECOND = 1_000_000_000
FREE = TAIFEXCost(instrument="TEST", commission_pts_per_side=0.0, tax_pts_per_side=0.0, point_value_nwd=10)
DAY1 = dt.date(2026, 4, 14)
DAY2 = dt.date(2026, 4, 15)


class ScriptedAlpha:
    """Returns ``script[i]`` on the i-th update (0 afterwards)."""

    def __init__(self, script: dict[int, float]) -> None:
        self.script = script
        self.calls = 0

    def reset(self) -> None:
        self.calls = 0

    def update(self, **_: object) -> float:
        value = self.script.get(self.calls, 0.0)
        self.calls += 1
        return value


def _day(tmp_path: Path, name: str, bids: np.ndarray, spread: float = 1.0, ts: np.ndarray | None = None) -> str:
    rows = np.zeros(len(bids), dtype=_DTYPE)
    rows["bid_px"] = bids
    rows["ask_px"] = bids + spread
    rows["bid_qty"] = 5.0
    rows["ask_qty"] = 5.0
    rows["mid_price"] = bids + spread / 2.0
    rows["local_ts"] = (np.arange(len(bids)) + 1) * _SECOND if ts is None else ts
    path = tmp_path / f"{name}.npy"
    np.save(path, rows)
    return ensure_hftbt_npz(str(path))


def _rising(n: int = 60) -> np.ndarray:
    # bid 100 for rows 0-9, 101 for 10-19, ...; ask is one point above.
    return 100.0 + (np.arange(n) // 10)


def _sim(alpha: ScriptedAlpha, **kw: object) -> TakerSim:
    params: dict = {"instrument": "TEST", "cost_model": FREE, "latency_us": 1000, "max_position": 5}
    params.update(kw)
    return TakerSim(alpha=alpha, **params)


def test_long_fills_at_the_ask_and_closes_at_the_bid(tmp_path):
    path = _day(tmp_path, "d1", _rising())
    day = _sim(ScriptedAlpha({4: 1.0})).run_day(DAY1, path)

    assert [(f.qty, f.price_pts) for f in day.fills] == [(1, 101.0)]  # bought at the ask, not the 100.5 mid
    assert day.residual_qty == 1
    assert day.close_price_pts == 105.0  # last bid
    assert day.gross_pts == pytest.approx(105.0 - 101.0)
    assert day.n_contracts == 2  # the entry plus the closing sale


def test_short_fills_at_the_bid_and_closes_at_the_ask(tmp_path):
    path = _day(tmp_path, "d1", _rising())
    day = _sim(ScriptedAlpha({4: -1.0})).run_day(DAY1, path)

    assert [(f.qty, f.price_pts) for f in day.fills] == [(-1, 100.0)]
    assert day.residual_qty == -1
    assert day.close_price_pts == 106.0  # last ask
    assert day.gross_pts == pytest.approx(100.0 - 106.0)


def test_round_trip_pays_the_spread_that_mid_to_mid_ignores(tmp_path):
    flat = np.full(40, 100.0)
    path = _day(tmp_path, "d1", flat, spread=2.0)
    day = _sim(ScriptedAlpha({4: 1.0, 20: -1.0})).run_day(DAY1, path)

    assert [(f.qty, f.price_pts) for f in day.fills] == [(1, 102.0), (-1, 100.0)]
    assert day.residual_qty == 0
    assert day.gross_pts == pytest.approx(-2.0)  # mid-to-mid on the same trades books 0.0


def test_cost_is_charged_per_contract_by_the_cost_profile(tmp_path):
    cost = TAIFEXCost(instrument="TEST", commission_pts_per_side=0.25, tax_pts_per_side=0.5, point_value_nwd=10)
    path = _day(tmp_path, "d1", np.full(40, 100.0))
    day = _sim(ScriptedAlpha({4: 1.0, 5: 1.0}), cost_model=cost).run_day(DAY1, path)

    assert day.n_contracts == 4  # two bought, two closed
    assert day.cost_pts == pytest.approx(4 * 0.75)
    assert day.net_pts == pytest.approx(day.gross_pts - 3.0)


def test_latency_moves_the_fill_to_the_later_book(tmp_path):
    path = _day(tmp_path, "d1", _rising())
    # The signal arrives on row 9 (bid 100, ask 101); with 1.5 s each way the order reaches the
    # exchange after row 10, where the ask is 102.
    fast = _sim(ScriptedAlpha({9: 1.0}), latency_us=1000).run_day(DAY1, path)
    slow = _sim(ScriptedAlpha({9: 1.0}), latency_us=1_500_000).run_day(DAY1, path)

    assert fast.fills[0].price_pts == 101.0
    assert slow.fills[0].price_pts == 102.0


def test_target_is_clipped_to_max_position(tmp_path):
    path = _day(tmp_path, "d1", np.full(60, 100.0))
    day = _sim(ScriptedAlpha({i: 1.0 for i in range(2, 40)}), max_position=2).run_day(DAY1, path)

    assert sum(f.qty for f in day.fills) == 2
    assert day.residual_qty == 2


def test_only_one_order_is_in_flight(tmp_path):
    path = _day(tmp_path, "d1", np.full(30, 100.0))
    # Steps on consecutive rows while each order takes 2.5 s to come back: the second step waits
    # for the first order instead of stacking a second one, then sends the difference.
    day = _sim(ScriptedAlpha({3: 1.0, 4: 1.0}), latency_us=1_250_000).run_day(DAY1, path)

    assert sum(f.qty for f in day.fills) == 2
    assert all(f.qty == 1 for f in day.fills)
    assert len(day.fills) == 2


def test_no_signal_means_no_trades(tmp_path):
    path = _day(tmp_path, "d1", _rising())
    day = _sim(ScriptedAlpha({})).run_day(DAY1, path)

    assert day.fills == ()
    assert day.gross_pts == 0.0
    assert day.n_contracts == 0


def test_days_are_independent_and_dated(tmp_path):
    p1 = _day(tmp_path, "d1", _rising())
    p2 = _day(tmp_path, "d2", np.full(40, 100.0))
    alpha = ScriptedAlpha({4: 1.0})
    result = _sim(alpha).run([(DAY1, p1), (DAY2, p2)])

    assert result.method == METHOD_ID
    assert [r["date"] for r in result.daily_rows()] == ["2026-04-14", "2026-04-15"]
    # reset() restarts the script, so day 2 trades too, and it starts flat
    assert result.days[1].fills[0].qty == 1
    assert result.total_net_pts == pytest.approx(sum(d.net_pts for d in result.days))
    assert result.sharpe == pytest.approx(daily_sharpe(result.daily_net_pts))


@pytest.mark.parametrize(
    "days, message",
    [
        ([], "no trading days"),
        ([(DAY1, "a"), (DAY1, "b")], "more than once"),
        ([(DAY2, "a"), (DAY1, "b")], "in order"),
    ],
)
def test_run_rejects_bad_day_lists(days, message):
    with pytest.raises(TakerSimError, match=message):
        _sim(ScriptedAlpha({})).run(days)


def test_daily_sharpe_uses_the_daily_series():
    assert daily_sharpe([1.0]) == 0.0
    assert daily_sharpe([2.0, 2.0, 2.0]) == 0.0
    values = [1.0, -1.0, 3.0]
    assert daily_sharpe(values) == pytest.approx(np.mean(values) / np.std(values) * np.sqrt(252))


class _FakeHbt:
    def __init__(self) -> None:
        self.state = SimpleNamespace(position=0.0, balance=0.0)

    def state_values(self, _asset: int) -> SimpleNamespace:
        return self.state


def _bound_bridge() -> tuple[_TakerOrderBridge, _FakeHbt]:
    bridge = _TakerOrderBridge(ScriptedAlpha({}), max_position=1, signal_threshold=0.3, symbol="X", price_scale=10_000)
    hbt = _FakeHbt()
    bridge.bind(hbt)
    return bridge, hbt


def test_cash_moving_without_a_fill_fails_closed():
    bridge, hbt = _bound_bridge()
    hbt.state = SimpleNamespace(position=0.0, balance=-0.5)  # a fee: exchange fees must be off
    with pytest.raises(TakerSimError, match="without a position change"):
        bridge._collect_fill(1)


def test_fill_without_a_price_fails_closed():
    bridge, hbt = _bound_bridge()
    hbt.state = SimpleNamespace(position=1.0, balance=0.0)
    with pytest.raises(TakerSimError, match="no usable price"):
        bridge._collect_fill(1)


def test_a_day_must_start_flat():
    bridge = _TakerOrderBridge(ScriptedAlpha({}), max_position=1, signal_threshold=0.3, symbol="X", price_scale=10_000)
    hbt = _FakeHbt()
    hbt.state = SimpleNamespace(position=1.0, balance=-100.0)
    with pytest.raises(TakerSimError, match="start flat"):
        bridge.bind(hbt)


def test_running_unbound_fails_closed():
    bridge = _TakerOrderBridge(ScriptedAlpha({}), max_position=1, signal_threshold=0.3, symbol="X", price_scale=10_000)
    with pytest.raises(TakerSimError, match="bind"):
        bridge.handle_event(None, object())


def test_first_signal_never_moves_the_target(tmp_path):
    path = _day(tmp_path, "d1", _rising())
    day = _sim(ScriptedAlpha({0: 1.0})).run_day(DAY1, path)

    assert day.fills == ()


def test_position_follows_the_native_runner_ladder_when_orders_fill(tmp_path):
    from research.backtest.hft_native_runner import _signals_to_positions

    rng = np.random.default_rng(7)
    script = {i: float(v) for i, v in enumerate(rng.choice([-1.0, 0.0, 1.0], size=50))}
    path = _day(tmp_path, "d1", np.full(50, 100.0))
    alpha = ScriptedAlpha(script)
    day = _sim(alpha, max_position=3).run_day(DAY1, path)

    # The signals the alpha actually returned, one per book event (a row can arrive as more
    # than one event, so this is not one per row).
    signals = np.asarray([script.get(i, 0.0) for i in range(alpha.calls)])
    ladder = _signals_to_positions(signals, 0.3, 3)
    # Orders (1 ms each way) settle before the next event, and a fill is booked on the event
    # after it, so the booked position equals the ladder one event earlier.
    assert day.residual_qty == int(ladder[-2])
    assert sum(f.qty for f in day.fills) == day.residual_qty


def test_one_sided_book_holds_the_order_until_both_sides_quote(tmp_path):
    rows = np.zeros(30, dtype=_DTYPE)
    rows["bid_px"][10:] = 100.0  # no bid for the first ten rows
    rows["ask_px"] = np.where(np.arange(30) < 10, 105.0, 101.0)
    rows["bid_qty"] = 5.0
    rows["ask_qty"] = 5.0
    rows["local_ts"] = (np.arange(30) + 1) * _SECOND
    np.save(tmp_path / "d1.npy", rows)
    path = ensure_hftbt_npz(str(tmp_path / "d1.npy"))

    day = _sim(ScriptedAlpha({4: 1.0})).run_day(DAY1, path)

    # The adapter emits no book event until both sides quote, so a day that opens one-sided
    # (as a split day can) never trades against the lone 105 offer.
    assert [(f.qty, f.price_pts) for f in day.fills] == [(1, 101.0)]


def test_rows_carry_the_sub_gate_fields(tmp_path):
    path = _day(tmp_path, "d1", _rising())
    row = _sim(ScriptedAlpha({4: 1.0})).run_day(DAY1, path).to_row()

    assert row["pnl_pts"] == row["net_pts"]
    assert row["fills"] == 2  # the entry and the closing sale
    assert row["residual_mtm_pts"] == 0.0


def _taipei_ns(text: str) -> int:
    moment = dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    return int(moment.timestamp()) * _SECOND


def _blocks(stamps: list[str], rows_each: int = 10) -> np.ndarray:
    return np.concatenate([_taipei_ns(t) + np.arange(rows_each) * _SECOND for t in stamps])


def test_split_assigns_night_sessions_to_the_next_trading_day(tmp_path):
    stamps = [
        "2026-04-13T15:30:00",  # Monday night -> Tuesday 04-14
        "2026-04-14T09:00:00",  # Tuesday day session -> 04-14
        "2026-04-14T15:30:00",  # Tuesday night -> Wednesday 04-15
        "2026-04-17T23:00:00",  # Friday night -> Monday 04-20
        "2026-04-18T02:00:00",  # after midnight on Saturday -> Monday 04-20
    ]
    ts = _blocks(stamps)
    path = _day(tmp_path, "multi", np.full(ts.size, 100.0), ts=ts)
    out_dir = tmp_path / "split"
    out_dir.mkdir()

    days = split_trading_days(path, str(out_dir))

    assert [d for d, _ in days] == [dt.date(2026, 4, 14), dt.date(2026, 4, 15), dt.date(2026, 4, 20)]
    with np.load(path) as whole:
        total = len(whole["data"])
    sizes = []
    for _, part in days:
        with np.load(part) as npz:
            sizes.append(len(npz["data"]))
    assert sum(sizes) == total  # no event dropped or duplicated
    assert sizes[0] == 2 * sizes[1]  # 04-14 holds two of the five blocks


def test_split_rejects_unordered_timestamps(tmp_path):
    ts = _blocks(["2026-04-14T09:00:00", "2026-04-13T15:30:00"])
    path = _day(tmp_path, "unordered", np.full(ts.size, 100.0), ts=ts)
    with pytest.raises(TakerSimError, match="not in order"):
        split_trading_days(path, str(tmp_path))


def test_split_rejects_dates_outside_the_calendar(tmp_path):
    path = _day(tmp_path, "epoch", np.full(20, 100.0))  # 1970 timestamps
    with pytest.raises(TakerSimError, match="outside"):
        split_trading_days(path, str(tmp_path))


def test_simulate_files_runs_every_trading_day(tmp_path):
    ts = _blocks(["2026-04-14T09:00:00", "2026-04-15T09:00:00", "2026-04-16T09:00:00"], rows_each=20)
    path = _day(tmp_path, "three", np.full(ts.size, 100.0), ts=ts)
    result = simulate_files(_sim(ScriptedAlpha({4: 1.0})), [path])

    assert [r["date"] for r in result.daily_rows()] == ["2026-04-14", "2026-04-15", "2026-04-16"]
    assert all(d.n_contracts == 2 for d in result.days)  # each day enters once and closes once


def test_simulate_files_rejects_a_day_split_across_files(tmp_path):
    a = _day(tmp_path, "a", np.full(20, 100.0), ts=_blocks(["2026-04-14T09:00:00"], rows_each=20))
    b = _day(tmp_path, "b", np.full(20, 100.0), ts=_blocks(["2026-04-14T10:00:00"], rows_each=20))
    with pytest.raises(TakerSimError, match="more than once"):
        simulate_files(_sim(ScriptedAlpha({})), [a, b])


def test_daily_sharpe_of_a_constant_daily_pnl_is_zero_not_float_noise() -> None:
    # np.std([1.1] * 30) is ~2e-16, not 0: dividing by it reported a Sharpe near 1e17.
    assert daily_sharpe([1.1] * 30) == 0.0
    assert daily_sharpe([0.1] * 7) == 0.0
    assert daily_sharpe([3.0] * 30) == 0.0


def test_a_fill_after_the_last_two_sided_book_is_booked_at_feed_end():
    bridge, hbt = _bound_bridge()
    hbt.state = SimpleNamespace(position=-1.0, balance=100.0)  # filled after the last book callback

    bridge.on_feed_end(7)

    assert [(f.ts_ns, f.qty, f.price_pts) for f in bridge.fills] == [(7, -1, 100.0)]
    assert bridge.exch_position == -1
    bridge.on_feed_end(8)  # nothing new: no second fill
    assert len(bridge.fills) == 1
