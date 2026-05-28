"""Slice B Task 2 + Task 3 tests for MakerEngine residual-MtM accounting.

Task 2 (cases 1-5 below): RED -> GREEN tests for the static helper
``MakerEngine._compute_residual_mtm``. The helper marks the un-FIFO'd residual
position to a chosen mark price. It is intentionally pure: callers select
which mark to pass (mid, last_trade, worse_of_mid_last_trade, etc.); the
helper is mark-agnostic.

Task 3 (case 6 below): MakerEngine-level integration test that runs one day
through ``MakerEngine.run`` with intentional unmatched residual and asserts
that the day-loop folds ``residual_mtm_pts`` into ``daily_pnl[]`` rows and
that ``equity_curve[-1]`` reflects the residual MtM.

Scale convention: matches `MakerEngine._compute_fifo_pnl` which uses
``scale = 1_000_000`` (golden parquet x1M scale, see CLAUDE.md / memory file
``feedback_golden_data_scale``). The plan code sketch
(``docs/superpowers/plans/2026-05-05-slice-b-maker-realism.md`` Section 6 Task 2)
illustratively wrote ``/10000.0`` - that is the platform-wide CLAUDE.md
convention, but inside this engine prices are x1M to match the data source.
The static helper accepts a ``scale`` parameter (default ``1_000_000``) so
both conventions work; the default keeps the helper consistent with
``_compute_fifo_pnl`` and Task 3's day-loop integration.

Test 4 deviation from the plan: the plan's RED case 4 phrasing
(``mark_method="worse_of_mid_last_trade"`` ... "should pick 99") would have
the helper itself perform mark selection. We chose the simpler
caller-picks-mark design - the helper just computes
``open_pos * (mark - avg)``; the caller is responsible for resolving the
worse mark. Test 4 verifies that contract: when the caller hands in the
worse mark (99 < mid 100, long position), the helper produces the
worse-case PnL.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from research.backtest.cost_models import CostModel
from research.backtest.fill_models import QueuePosition
from research.backtest.maker_engine import (
    Hold,
    MakerEngine,
    PostQuote,
    TickData,
)

SCALE = 1_000_000  # x1M - matches _compute_fifo_pnl in maker_engine.py


def _pts(p: float) -> int:
    """Helper: convert a float price-in-points to scaled int."""
    return int(round(p * SCALE))


# ---------------------------------------------------------------------------
# Case 1: open_pos=0 -> returns 0.0 regardless of mark
# ---------------------------------------------------------------------------
def test_residual_mtm_zero_position_returns_zero() -> None:
    """Zero residual position has zero MtM regardless of mark/avg."""
    result = MakerEngine._compute_residual_mtm(
        open_pos=0,
        mark_price=_pts(123.45),
        avg_entry_price=_pts(100.00),
        mark_method="last_mid",
    )
    assert result == 0.0

    # Also verify with extreme mark/avg disparity - still zero.
    result_extreme = MakerEngine._compute_residual_mtm(
        open_pos=0,
        mark_price=_pts(99999.0),
        avg_entry_price=_pts(1.0),
        mark_method="last_mid",
    )
    assert result_extreme == 0.0


# ---------------------------------------------------------------------------
# Case 2: long +1, mark = avg + 50 pts -> +50.0
# ---------------------------------------------------------------------------
def test_residual_mtm_long_one_lot_mark_above_avg() -> None:
    """Long 1 lot with mark 50 pts above avg yields +50.0 points."""
    avg = _pts(100.0)
    mark = _pts(150.0)  # 50 points above avg
    result = MakerEngine._compute_residual_mtm(
        open_pos=+1,
        mark_price=mark,
        avg_entry_price=avg,
        mark_method="last_mid",
    )
    assert result == pytest.approx(50.0, rel=1e-9, abs=1e-9)


# ---------------------------------------------------------------------------
# Case 3: short -1, mark = avg + 50 pts -> -50.0 (short loses)
# ---------------------------------------------------------------------------
def test_residual_mtm_short_one_lot_mark_above_avg() -> None:
    """Short 1 lot with mark 50 pts above avg yields -50.0 (short loses)."""
    avg = _pts(100.0)
    mark = _pts(150.0)  # 50 pts above avg -> bad for short
    result = MakerEngine._compute_residual_mtm(
        open_pos=-1,
        mark_price=mark,
        avg_entry_price=avg,
        mark_method="last_mid",
    )
    assert result == pytest.approx(-50.0, rel=1e-9, abs=1e-9)


# ---------------------------------------------------------------------------
# Case 4: caller-picks-mark contract under worse_of_mid_last_trade
# ---------------------------------------------------------------------------
def test_residual_mtm_caller_picks_worse_mark_for_long() -> None:
    """Helper is mark-agnostic - caller resolves which mark to pass.

    Setup: long 1 lot at avg=100. Mid=100, last_trade=99. The worse mark for
    a long is the lower one (99). The CALLER selects 99 and passes it; the
    helper just computes ``open_pos * (mark - avg)``. Result: -1.0 point.

    This locks in the design decision (caller-picks-mark) made vs. the
    plan's wording. See module docstring for rationale.
    """
    avg = _pts(100.0)
    mid = _pts(100.0)
    last_trade = _pts(99.0)
    # Caller's worse-of-mid-last-trade resolution for a long = min(mid, last)
    worse_mark = min(mid, last_trade)
    assert worse_mark == last_trade  # sanity: 99 < 100

    result = MakerEngine._compute_residual_mtm(
        open_pos=+1,
        mark_price=worse_mark,
        avg_entry_price=avg,
        mark_method="worse_of_mid_last_trade",
    )
    assert result == pytest.approx(-1.0, rel=1e-9, abs=1e-9)


# ---------------------------------------------------------------------------
# Case 5: float-precision regression - 10 lots x 1-unit diff = exact 1.0e-5
# ---------------------------------------------------------------------------
def test_residual_mtm_float_precision_no_artifacts() -> None:
    """Integer-divide-then-cast must produce exact 1.0e-5, not 9.999...e-6.

    With open_pos=+10 and mark - avg = 1 scaled-int unit (sub-tick at
    scale=1e6, but a useful precision probe):
      pnl_int = 10 * 1 = 10
      pnl_pts = 10 / 1_000_000 = 1.0e-5

    Pinning the exact value rules out e.g. 9.99...e-6 artifacts that would
    occur if the helper accidentally did float math along the way.
    """
    # Construct prices so mark - avg = 1 (one scaled-int unit).
    avg = _pts(100.0)
    mark = avg + 1
    result = MakerEngine._compute_residual_mtm(
        open_pos=+10,
        mark_price=mark,
        avg_entry_price=avg,
        mark_method="last_mid",
    )
    expected = 10 / SCALE  # = 1.0e-5 with SCALE=1_000_000
    assert result == expected, f"Float-precision regression: expected exact {expected!r}, got {result!r}"


# ---------------------------------------------------------------------------
# Case 6 (Task 3): MakerEngine-level integration — residual MtM folded into
# daily_pnl + equity_curve via the day loop.
# ---------------------------------------------------------------------------
@dataclass
class _DeterministicFillModel:
    """Always fills once a trade sweeps the order price, ignoring queue."""

    label: str = "deterministic"

    def post_quote(self, side: str, price: int, queue_ahead: int) -> QueuePosition:
        return QueuePosition(side=side, price=price, queue_ahead=0)

    def check_fills(self, orders, trade_price, trade_volume):
        for o in orders:
            if o.side == "buy" and trade_price <= o.price:
                return True
            if o.side == "sell" and trade_price >= o.price:
                return True
        return False


@dataclass
class _ZeroCost(CostModel):
    label: str = "zero"

    def apply(self, gross, n_fills):
        return gross


class _BuyOnceStrategy:
    """Posts a single BUY at best bid on the first bidask, then holds.

    Designed to leave an unmatched +1 residual long position at end of day.
    """

    def __init__(self) -> None:
        self._posted = False

    def on_tick(self, tick: TickData):
        if self._posted or tick.is_trade:
            return [Hold()]
        self._posted = True
        return [PostQuote(side="buy", price=tick.bid_price, qty=1)]

    def on_fill(self, side, price, mid_price) -> None:
        pass


def _residual_long_events() -> list[TickData]:
    """One-day fixture: places a BUY at 100, fills it via a 99 trade, ends
    with bid=100/ask=120 -> last_mid = 110. Final position = +1 long with
    avg_entry = 100. Residual MtM = +1 * (110 - 100) = +10.0 points.
    """
    scale = 1_000_000
    return [
        # Initial bid/ask -> strategy posts BUY @100
        TickData(
            exch_ts=1_000_000_000,
            bid_price=100 * scale,
            ask_price=102 * scale,
            bid_qty=10,
            ask_qty=10,
            trade_price=0,
            trade_volume=0,
            is_trade=False,
            scale=scale,
        ),
        # Trade at 99 -> fills the BUY @100 (deterministic fill model)
        TickData(
            exch_ts=1_100_000_000,
            bid_price=100 * scale,
            ask_price=102 * scale,
            bid_qty=10,
            ask_qty=10,
            trade_price=99 * scale,
            trade_volume=5,
            is_trade=True,
            scale=scale,
        ),
        # Final bidask: bid=100/ask=120 -> last_mid = 110
        TickData(
            exch_ts=1_200_000_000,
            bid_price=100 * scale,
            ask_price=120 * scale,
            bid_qty=10,
            ask_qty=10,
            trade_price=0,
            trade_volume=0,
            is_trade=False,
            scale=scale,
        ),
    ]


class _FakeCKSource:
    """Stub ClickHouseSource that returns canned events. Avoids __slots__
    patching limitation on the real ClickHouseSource."""

    def __init__(self, events: list[TickData]) -> None:
        self._events = events
        # Mirror the real source's host/port attrs that MakerEngine.run
        # references for ``data_source`` metadata.
        self._host = "fake"
        self._port = 0

    def health_check(self) -> None:
        return None

    def load_day(self, symbol: str, date: str) -> list[TickData]:
        return list(self._events)

    def available_dates(self, symbol: str) -> list[str]:
        return ["2026-05-05"]


def test_run_folds_residual_mtm_into_daily_pnl_and_equity_curve() -> None:
    """End-to-end: a day with +1 unmatched long residual must surface
    ``residual_mtm_pts != 0`` in ``daily_pnl[0]`` and the equity curve must
    reflect it (gross + residual_mtm rather than gross alone)."""
    fixture_events = _residual_long_events()
    engine = MakerEngine(
        fill_model=_DeterministicFillModel(),
        cost_model=_ZeroCost(),
        ck_source=_FakeCKSource(fixture_events),  # type: ignore[arg-type]
    )

    result = engine.run(
        strategy=_BuyOnceStrategy(),
        instrument="TEST",
        dates=["2026-05-05"],
        pipeline_mode="strict",
    )

    # daily_pnl row sanity
    assert result.daily_pnl is not None
    assert len(result.daily_pnl) == 1
    row = result.daily_pnl[0]

    # Day produced 1 fill, final position +1, no FIFO trips closed.
    assert row["fills"] == 1
    assert row["final_pos"] == 1
    assert row["trips"] == 0
    # Residual MtM: +1 * (last_mid 110 - avg_entry 100) = +10.0 pts.
    assert row["residual_mtm_pts"] == pytest.approx(10.0, rel=1e-9, abs=1e-9)
    assert row["residual_qty"] == 1
    assert row["mark_method"] == "last_mid"
    # gross alone is 0 (no FIFO close); MtM-aware day_pnl_pts == residual.
    assert row["gross_pts"] == pytest.approx(0.0, abs=1e-9)
    assert row["pnl_pts"] == pytest.approx(10.0, rel=1e-9, abs=1e-9)

    # equity_curve must end at residual MtM (+10.0 pts).
    assert result.equity_curve[-1] == pytest.approx(10.0, rel=1e-9, abs=1e-9)


# ---------------------------------------------------------------------------
# Case 7 (Task 4): BacktestResult-level aggregation of residual fields.
#
# Two-day fixture: each day independently posts a single BUY that fills via a
# trade and ends long +1 with last_mid=110, avg=100 -> residual_mtm=+10.0 pts.
# Aggregation policy (decided after Task 3):
#   * residual_mtm_pts -> SUM across days (mirrors total_gross accumulation)
#   * residual_qty     -> final-day snapshot (per-day independent FIFO)
#   * mark_method      -> single string (single-policy design)
# ---------------------------------------------------------------------------
class _BuyOnceStrategyMultiDay:
    """Resets ``_posted`` per call to ``on_tick`` only at start of fixture day.

    Each day's events come fresh from the fake CK source, but the strategy
    instance persists across days inside MakerEngine.run. To keep both days
    independent we re-arm by detecting a fresh first bidask of a new day -
    simpler approach: just count how many BUYs we've posted (max one per day)
    using the same logic as ``_BuyOnceStrategy`` but with a per-day counter
    we reset whenever we see a non-trade event with the FIRST exch_ts of a
    new day. Even simpler: track day boundary via ``_seen_dates`` keyed on a
    coarse timestamp slot. Simplest of all: reset whenever ``on_tick`` sees
    ``is_trade == False`` AND we've already posted.
    """

    def __init__(self) -> None:
        self._posted = False
        self._last_ts = -1

    def on_tick(self, tick: TickData):
        # New day boundary heuristic: a non-trade event whose ts is much
        # earlier than our last seen ts (the fixture rewinds clocks per day).
        if not tick.is_trade and tick.exch_ts < self._last_ts:
            self._posted = False
        self._last_ts = max(self._last_ts, tick.exch_ts)

        if self._posted or tick.is_trade:
            return [Hold()]
        self._posted = True
        return [PostQuote(side="buy", price=tick.bid_price, qty=1)]

    def on_fill(self, side, price, mid_price) -> None:
        pass


class _FakeCKSourceMultiDay:
    """Returns the same residual-long events for each requested date."""

    def __init__(self, events_per_day: list[TickData], dates: list[str]) -> None:
        self._events = events_per_day
        self._dates = list(dates)
        self._host = "fake"
        self._port = 0

    def health_check(self) -> None:
        return None

    def load_day(self, symbol: str, date: str) -> list[TickData]:
        # Each call returns a fresh copy so day loops do not share mutable refs.
        return list(self._events)

    def available_dates(self, symbol: str) -> list[str]:
        return list(self._dates)


def test_backtest_result_aggregates_residual_fields() -> None:
    """Two-day run -> BacktestResult.residual_mtm_pts == sum(daily residuals),
    residual_qty == last day's residual, mark_method == default 'last_mid'."""
    fixture_events = _residual_long_events()
    dates = ["2026-05-05", "2026-05-06"]
    engine = MakerEngine(
        fill_model=_DeterministicFillModel(),
        cost_model=_ZeroCost(),
        ck_source=_FakeCKSourceMultiDay(fixture_events, dates),  # type: ignore[arg-type]
    )

    result = engine.run(
        strategy=_BuyOnceStrategyMultiDay(),
        instrument="TEST",
        dates=dates,
        pipeline_mode="strict",
    )

    # Two daily rows, each with +10.0 residual_mtm.
    assert result.daily_pnl is not None
    assert len(result.daily_pnl) == 2
    daily_residuals_sum = sum(d["residual_mtm_pts"] for d in result.daily_pnl)
    assert daily_residuals_sum == pytest.approx(20.0, rel=1e-9, abs=1e-9)

    # Aggregation contract:
    # residual_mtm_pts -> sum across days (rounded to 2dp like daily rows).
    assert result.residual_mtm_pts == round(daily_residuals_sum, 2)
    # residual_qty -> final-day snapshot.
    assert result.residual_qty == result.daily_pnl[-1]["residual_qty"]
    assert result.residual_qty == 1
    # mark_method -> default policy.
    assert result.mark_method == "last_mid"
