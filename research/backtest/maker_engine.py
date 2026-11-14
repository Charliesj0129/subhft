"""MakerEngine — CK-direct queue depletion backtest for maker strategies.

Extracted and generalized from research/tools/r47_ck_direct_backtest_v2.py.
Strategy logic is injected via MakerStrategy protocol — engine handles
market simulation, fill determination, and PnL accounting.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
import re
from collections.abc import Callable, Sequence
from datetime import date as calendar_date
from datetime import datetime, timezone
from functools import partial
from typing import TYPE_CHECKING, Protocol

import numpy as np
import structlog

from hft_platform.contracts.maker_actions import CancelQuote, Hold, PostQuote
from research.backtest.cost_models import CostModel
from research.backtest.fill_models import FillModel, QueuePosition
from research.backtest.trade_pnl_projector import project_trade_pnl
from research.backtest.types import BacktestResult
from research.data_pipeline.reader import CK_PRICE_SCALE, GovernedReader, WindowRead

if TYPE_CHECKING:
    from research.backtest.rolled_series import RolledSchedule

logger = structlog.get_logger()

_ROOT = re.compile(r"^[A-Z]{2,4}$")
_TZ_NAME = re.compile(r"^[A-Za-z_]+/[A-Za-z_]+$")

# One part of a day on one contract: the contract and how to load its events.
_Segment = tuple[str, Callable[[], "list[TickData]"]]
# One day of a series: its row label and its segments (two on a rolled delivery day).
_SeriesDay = tuple[str, tuple[_Segment, ...]]


class MissingSeriesData(ValueError):
    """A rolled series segment holds no events of the contract it quotes."""


@dataclass(frozen=True)
class TickData:
    """Single market event (bidask update or trade)."""

    exch_ts: int
    bid_price: int
    ask_price: int
    bid_qty: int
    ask_qty: int
    trade_price: int
    trade_volume: int
    is_trade: bool
    scale: int = 1_000_000

    @property
    def spread_pts(self) -> int:
        return (self.ask_price - self.bid_price) // self.scale

    @property
    def mid_price(self) -> float:
        return (self.bid_price + self.ask_price) / (2 * self.scale)


# Same convention as ``HftNativeRunner`` (``BacktestConfig.is_oos_split``): the first 70% of the
# ordered observations are in-sample, the rest out-of-sample.
DEFAULT_IS_OOS_SPLIT = 0.7
# ``sharpe_oos`` feeds floors such as ``discover_gate_d_candidates(min_sharpe_oos=1.0)``. An annualised
# Sharpe over n daily returns has a standard error near sqrt(252 / n): about 3.5 at 20 days, so a floor of
# 1.0 would be cleared by luck. Until the OOS segment has this many days the engine keeps reporting 0.0
# (the value those floors have always seen) and says why in ``sharpe_oos_status``; the raw figure is
# still published as ``sharpe_oos_raw`` for people. Fixing the hard-coded 0.0 must not make a gate easier.
MIN_OOS_DAYS = 20


@dataclass(frozen=True)
class OosSharpe:
    """Out-of-sample Sharpe as measured (``raw``) and as reported to gates (``reported``)."""

    raw: float
    reported: float
    days: int
    status: str  # "ok" | "insufficient_oos_days" | "flat_oos_returns"


@dataclass(frozen=True)
class LatencyProfile:
    """D5 (2026-04-21 incident): broker latency model for live-faithful backtest.

    ``place_ns``: nanoseconds between ``PostQuote`` returned by the strategy
    and the order becoming visible (fillable) on the book. Zero = instant RTT.

    ``cancel_ns``: nanoseconds between ``CancelQuote`` and the order being
    removed from the book. During this window trades can still adversely
    fill the order that is "in flight to cancel".

    Real-world example — Shioaji sim API today shows P95 RTT ~800 ms
    (``docs/architecture/latency-baseline-shioaji-sim-vs-system.md``). Use
    :meth:`shioaji_p95` for a canned profile.
    """

    place_ns: int = 0
    cancel_ns: int = 0
    # Name of the profile in config/research/latency_profiles.yaml that these numbers came
    # from. Empty when the profile was built by hand; the result then says "unlabelled".
    profile_id: str = ""

    @classmethod
    def shioaji_p95(cls) -> "LatencyProfile":
        """Shioaji sim API P95 RTT (800 ms) applied symmetrically."""
        return cls(place_ns=800_000_000, cancel_ns=800_000_000)


class MakerStrategy(Protocol):
    """Strategy decides when/where to quote. Engine decides fills."""

    def on_tick(self, tick: TickData) -> list[PostQuote | CancelQuote | Hold]: ...

    def on_fill(self, side: str, price: int, mid_price: float) -> None: ...


class ClickHouseSource:
    """Tick + BidAsk events from ClickHouse through the governed reader (research v2 C1).

    Every read goes through :class:`research.data_pipeline.reader.GovernedReader`: the session
    rule, duplicate-delivery removal and trading-day windows are applied there, and what each
    read removed is kept in :attr:`reads`. A date is a trading day: the night session before it,
    then its day session.
    """

    __slots__ = ("_host", "_port", "_user", "_password", "_reader", "_reads")

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        password: str | None = None,
        *,
        reader: GovernedReader | None = None,
    ) -> None:
        self._host = host or os.environ.get("CLICKHOUSE_HOST", "localhost")
        self._port = port or int(os.environ.get("CLICKHOUSE_PORT", "8123"))
        self._user = os.environ.get("CLICKHOUSE_USER", "default")
        self._password = password or os.environ.get("CLICKHOUSE_PASSWORD", "")
        self._reader = reader
        self._reads: list[dict] = []

    @property
    def reads(self) -> tuple[dict, ...]:
        """Provenance of every window read so far: rows fetched, session-filtered, deduplicated."""
        return tuple(self._reads)

    def _governed(self) -> GovernedReader:
        if self._reader is None:
            self._reader = GovernedReader.connect(
                host=self._host, port=self._port, user=self._user, password=self._password
            )
        return self._reader

    def health_check(self) -> None:
        try:
            self._governed().ping()
        except Exception as exc:
            raise ConnectionError(
                f"ClickHouse not reachable at http://{self._host}:{self._port}/. "
                f"Please start it: docker compose up -d clickhouse\n"
                f"Original error: {exc}"
            ) from exc

    def load_day(self, symbol: str, date: str) -> list[TickData]:
        """Events of trading day ``date``: the night session before it, then its day session."""
        return self._events(self._governed().read_trading_day(symbol, calendar_date.fromisoformat(date)))

    def load_window(self, symbol: str, start_ns: int, end_ns: int) -> list[TickData]:
        """Events with ``start_ns <= exch_ts < end_ns``, in session and deduplicated."""
        return self._events(self._governed().read_window(symbol, int(start_ns), int(end_ns)))

    def _events(self, read: WindowRead) -> list[TickData]:
        self._reads.append(read.provenance())
        scale = CK_PRICE_SCALE
        events: list[TickData] = []
        for row in read.rows:
            if row[0] == "BidAsk":
                bids_price, asks_price, bids_vol, asks_vol = row[3], row[4], row[5], row[6]
                if not bids_price or not asks_price:
                    continue
                events.append(
                    TickData(
                        exch_ts=int(row[1]),
                        bid_price=int(bids_price[0]),
                        ask_price=int(asks_price[0]),
                        bid_qty=int(bids_vol[0]),
                        ask_qty=int(asks_vol[0]),
                        trade_price=0,
                        trade_volume=0,
                        is_trade=False,
                        scale=scale,
                    )
                )
            elif row[0] == "Tick":
                events.append(
                    TickData(
                        exch_ts=int(row[1]),
                        bid_price=0,
                        ask_price=0,
                        bid_qty=0,
                        ask_qty=0,
                        trade_price=int(row[7] or 0),
                        trade_volume=int(row[8] or 0),
                        is_trade=True,
                        scale=scale,
                    )
                )
        return events

    def available_dates(self, symbol: str) -> list[str]:
        """Trading days whose window holds an event of ``symbol``."""
        return [day.isoformat() for day in self._governed().trading_days(symbol)]

    def available_local_slots(self, root: str, tz: str, split_minute: int) -> list[tuple[calendar_date, bool]]:
        """``(local date, at or after split_minute)`` pairs in ``tz`` holding an event of any ``<root>`` month.

        ``split_minute`` is a minute of the local day; the rolled series dates an
        event by which side of it the event falls on (``TMFK6`` ... any month).
        """
        if not _ROOT.match(root):
            raise ValueError(f"contract root must be 2-4 capital letters, got {root!r}")
        if not _TZ_NAME.match(tz):
            raise ValueError(f"not a timezone name: {tz!r}")
        if not 0 <= int(split_minute) < 24 * 60:
            raise ValueError(f"split_minute must be a minute of the day, got {split_minute!r}")
        return self._governed().local_slots(tz=tz, split_minute=int(split_minute), root=root)


class MakerEngine:
    """CK-direct maker backtest engine."""

    __slots__ = (
        "_fill_model",
        "_cost_model",
        "_ck_source",
        "_latency",
        "_is_oos_split",
        "_mark_method",
        "_last_mid",
        "_last_avg_entry",
    )

    def __init__(
        self,
        fill_model: FillModel,
        cost_model: CostModel,
        ck_source: ClickHouseSource | None = None,
        latency_profile: LatencyProfile | None = None,
        mark_method: str = "last_mid",
        is_oos_split: float = DEFAULT_IS_OOS_SPLIT,
    ) -> None:
        if not 0.0 < is_oos_split < 1.0:
            raise ValueError(f"is_oos_split must be in (0, 1), got {is_oos_split}")
        self._is_oos_split = is_oos_split
        self._fill_model = fill_model
        self._cost_model = cost_model
        self._ck_source = ck_source or ClickHouseSource()
        # D5: None = instant-RTT (backward compat). Set for live-faithful sim.
        self._latency = latency_profile
        # Slice B Task 3: mark-to-market policy for residual position. The
        # day loop computes ``last_mid`` and the FIFO-residual avg entry and
        # passes them through the static ``_compute_residual_mtm`` helper.
        # ``mark_method`` is currently advisory (recorded in daily_pnl rows).
        self._mark_method = mark_method
        self._last_mid: int = 0
        self._last_avg_entry: int = 0

    @property
    def engine_type(self) -> str:
        return "maker"

    @property
    def fill_model_name(self) -> str:
        return self._fill_model.label

    def run(
        self,
        strategy: MakerStrategy,
        instrument: str,
        dates: list[str] | None = None,
        pipeline_mode: str = "strict",
    ) -> BacktestResult:
        self._ck_source.health_check()

        if dates is None:
            dates = self._ck_source.available_dates(instrument)
        if not dates:
            raise ValueError(f"No data available for {instrument}")

        days = [(d, ((instrument, partial(self._ck_source.load_day, instrument, d)),)) for d in dates]
        return self._run_series(strategy, days, instrument=instrument, pipeline_mode=pipeline_mode, rolled=False)

    def run_rolled(
        self,
        strategy: MakerStrategy,
        schedule: RolledSchedule,
        pipeline_mode: str = "strict",
    ) -> BacktestResult:
        """Run one series across contract months, one row per trading day (ADR-004).

        Each trading day loads its own window on the contract the schedule
        quotes, in two segments on a delivery day. Segments are independent
        here as days are in :meth:`run`, so no position crosses a roll; a
        strategy with ``set_active_symbol`` is told the new contract before
        the first event on it. A segment without events raises
        :class:`MissingSeriesData`: a missing month must not shrink the series.
        """
        self._ck_source.health_check()
        if not schedule.days:
            raise ValueError(f"No trading days for {schedule.instrument}")
        days = [
            (
                d.trading_day.isoformat(),
                tuple(
                    (seg.symbol, partial(self._ck_source.load_window, seg.symbol, seg.start_ns, seg.end_ns))
                    for seg in d.segments
                ),
            )
            for d in schedule.days
        ]
        return self._run_series(
            strategy, days, instrument=schedule.instrument, pipeline_mode=pipeline_mode, rolled=True
        )

    def _run_series(
        self,
        strategy: MakerStrategy,
        days: Sequence[_SeriesDay],
        *,
        instrument: str,
        pipeline_mode: str,
        rolled: bool,
    ) -> BacktestResult:
        daily_pnl: list[dict] = []
        equity_points: list[float] = [0.0]
        total_gross = 0.0
        total_fills = 0
        spread_breakdown: dict[int, dict] = {}
        # Slice B Task 4: aggregate residual fields for BacktestResult.
        # ``total_residual_mtm`` SUMS each day's residual_mtm_pts (mirrors the
        # ``total_gross`` accumulation; the equity curve already reflects it).
        # ``final_residual_qty`` snapshots the LAST traded day's residual qty
        # (per-day FIFO is independent so day-to-day residual qty is not
        # additive). Both default to 0 if no days traded.
        total_residual_mtm = 0.0
        final_residual_qty = 0
        # Round 24/25: accumulate per-round-trip NET PnL across days so the
        # trade-axis sub-gates (trade_concentration, outlier_trade_removal,
        # edge_per_round_trip) read goal §1's per-round-trip edge directly
        # instead of falling back to daily_pnl.  Per-trip net allocates the
        # day-level cost/MtM delta evenly across that day's trips so the
        # invariant ``sum(day-trips) == day_net`` holds whenever day_trips > 0.
        trade_pnl_pts: list[float] = []

        active_contract: str | None = None
        set_active_symbol = getattr(strategy, "set_active_symbol", None)
        # _run_day starts every day flat with no resting orders; a strategy
        # that keeps its own position is flattened to match before each one.
        flatten_position = getattr(strategy, "flatten_position", None)
        for date, segments in days:
            loaded = [(contract, load_events()) for contract, load_events in segments]
            if rolled:
                for contract, events in loaded:
                    if not events:
                        raise MissingSeriesData(f"{instrument} {date}: no events of {contract}")
            elif not loaded[0][1]:
                continue

            day_fills: list[dict] = []
            day_gross = 0.0
            day_trips = day_wins = 0
            day_residual_mtm = 0.0
            day_trip_grosses: list[float] = []
            seg_rows: list[dict] = []
            for contract, events in loaded:
                if rolled and contract != active_contract:
                    if callable(set_active_symbol):
                        set_active_symbol(contract)
                    active_contract = contract
                if callable(flatten_position):
                    flatten_position()

                seg_fills, day_position, seg_last_mid, seg_last_avg = self._run_day(strategy, events)
                seg_gross, seg_trips, seg_wins = self._compute_fifo_pnl(seg_fills)
                # Slice B Task 3: residual MtM folded into day-level accounting.
                seg_residual_mtm = self._compute_residual_mtm(
                    open_pos=day_position,
                    mark_price=seg_last_mid,
                    avg_entry_price=seg_last_avg,
                    mark_method=self._mark_method,
                )
                day_fills.extend(seg_fills)
                day_gross += seg_gross
                day_trips += seg_trips
                day_wins += seg_wins
                day_residual_mtm += seg_residual_mtm
                # Trips never pair fills across contracts.
                day_trip_grosses.extend(project_trade_pnl(seg_fills))
                seg_rows.append(
                    {
                        "contract": contract,
                        "fills": len(seg_fills),
                        "gross_pts": round(seg_gross, 2),
                        "final_pos": day_position,
                        "residual_mtm_pts": round(seg_residual_mtm, 2),
                    }
                )
            # Punch-list (2026-05-29): preserve signed residual_qty
            # (positive=long, negative=short) for accounting; expose
            # ``abs_residual_qty`` as the derived display/aggregate field.
            day_residual_qty_signed = int(day_position)
            day_abs_residual_qty = abs(day_residual_qty_signed)
            day_gross_mtm_aware = day_gross + day_residual_mtm
            day_net = self._cost_model.apply(day_gross_mtm_aware, len(day_fills))

            total_gross += day_gross_mtm_aware
            # Slice B Task 4: track residual aggregation alongside total_gross.
            # Sum the rounded daily values so the result-level field matches
            # ``round(sum(d["residual_mtm_pts"] for d in daily_pnl), 2)``.
            total_residual_mtm += round(day_residual_mtm, 2)
            final_residual_qty = day_residual_qty_signed
            total_fills += len(day_fills)
            equity_points.append(equity_points[-1] + day_net)

            daily_pnl.append(
                {
                    "date": date,
                    "pnl_pts": round(day_net, 2),
                    "gross_pts": round(day_gross, 2),
                    "fills": len(day_fills),
                    "trips": day_trips,
                    "wins": day_wins,
                    "final_pos": day_position,
                    "residual_mtm_pts": round(day_residual_mtm, 2),
                    "residual_qty": day_residual_qty_signed,
                    "abs_residual_qty": day_abs_residual_qty,
                    "mark_method": self._mark_method,
                }
            )
            if rolled:
                daily_pnl[-1]["contract"] = seg_rows[0]["contract"]
                if len(seg_rows) > 1:
                    daily_pnl[-1]["segments"] = seg_rows

            for f in day_fills:
                spr = f.get("spread_pts", 0)
                if spr not in spread_breakdown:
                    spread_breakdown[spr] = {"fills": 0, "gross_pnl": 0.0}
                spread_breakdown[spr]["fills"] += 1

            # Round 25: project per-round-trip PnL from this day's fills,
            # allocate day-level (cost + residual MtM) delta evenly across
            # trips so sum-per-day == day_net (when day_trips > 0).
            if day_trip_grosses:
                delta_per_trip = (day_net - day_gross) / len(day_trip_grosses)
                trade_pnl_pts.extend(g + delta_per_trip for g in day_trip_grosses)

        equity = np.array(equity_points)
        total_net = self._cost_model.apply(total_gross, total_fills)
        n_days = len(daily_pnl)
        winning_days = sum(1 for d in daily_pnl if d["pnl_pts"] > 0)
        daily_returns = np.diff(equity)

        sharpe = 0.0
        if len(daily_returns) > 1 and np.std(daily_returns) > 0:
            sharpe = float(np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(252))
        oos = self._oos_sharpe(daily_returns, self._is_oos_split)

        max_dd = 0.0
        peak = equity[0]
        for val in equity:
            peak = max(peak, val)
            dd = (peak - val) / max(abs(peak), 1e-9)
            max_dd = max(max_dd, dd)

        pnl_per_fill = total_net / total_fills if total_fills > 0 else 0.0
        qf = getattr(self._fill_model, "queue_fraction", None)

        return BacktestResult(
            signals=np.array([]),
            equity_curve=equity,
            positions=np.array([]),
            sharpe_is=sharpe,
            sharpe_oos=oos.reported,
            ic_series=np.array([]),
            ic_mean=0.0,
            ic_std=0.0,
            ic_tstat=0.0,
            ic_pvalue=1.0,
            ic_halflife=0,
            sortino=0.0,
            cvar_5pct=0.0,
            turnover=0.0,
            max_drawdown=max_dd,
            regime_metrics={},
            capacity_estimate=0.0,
            run_id=str(uuid.uuid4())[:12],
            config_hash="",
            latency_profile=self._latency_report(),
            engine_type="maker",
            fill_model=self._fill_model.label,
            cost_model=self._cost_model.label,
            instrument=instrument,
            data_period=f"{days[0][0]}..{days[-1][0]}" if days else "",
            data_source=f"clickhouse://{self._ck_source._host}:{self._ck_source._port}/hft",
            pipeline_mode=pipeline_mode,
            created_at=datetime.now(timezone.utc).isoformat(),
            queue_fraction=qf,
            maker_scorecard={
                "total_pnl_pts": round(total_net, 2),
                "total_fills": total_fills,
                "pnl_per_fill": round(pnl_per_fill, 4),
                "winning_days": winning_days,
                "winning_day_pct": (round(winning_days / n_days * 100, 1) if n_days > 0 else 0),
                "n_days": n_days,
                "oos_days": oos.days,
                "is_oos_split": self._is_oos_split,
                "sharpe_oos_raw": oos.raw,
                "sharpe_oos_status": oos.status,
            },
            per_spread_breakdown={str(k): v for k, v in sorted(spread_breakdown.items())},
            # Slice B Task 4 + 2026-05-29 punch list: residual decomposition.
            # ``residual_qty`` carries the signed final-day position; the
            # unsigned magnitude is exposed via ``abs_residual_qty``.
            residual_mtm_pts=round(total_residual_mtm, 2),
            residual_qty=final_residual_qty,
            abs_residual_qty=abs(final_residual_qty),
            mark_method=self._mark_method,
            trade_pnl=trade_pnl_pts if trade_pnl_pts else None,
            daily_pnl=daily_pnl,
        )

    @staticmethod
    def _oos_sharpe(daily_returns: np.ndarray, split: float) -> "OosSharpe":
        """Annualised Sharpe of the chronologically last ``1 - split`` of the daily returns.

        ``raw`` is the figure whenever the segment has at least two days that vary; ``reported``
        is ``raw`` only from ``MIN_OOS_DAYS`` days on, else 0.0. Neither is ever NaN, which would
        slip past ``sharpe_oos < floor`` filters.
        """
        n = len(daily_returns)
        if n < 2:
            return OosSharpe(raw=0.0, reported=0.0, days=0, status="insufficient_oos_days")
        split_idx = max(1, min(n - 1, int(n * split)))
        oos = np.asarray(daily_returns[split_idx:], dtype=float)
        days = len(oos)
        raw = float(np.mean(oos) / np.std(oos) * np.sqrt(252)) if days >= 2 and np.std(oos) > 0 else 0.0
        if days < MIN_OOS_DAYS:
            return OosSharpe(raw=raw, reported=0.0, days=days, status="insufficient_oos_days")
        if not np.std(oos) > 0:
            return OosSharpe(raw=0.0, reported=0.0, days=days, status="flat_oos_returns")
        return OosSharpe(raw=raw, reported=raw, days=days, status="ok")

    def _latency_report(self) -> dict:
        """What latency the run actually used. Instant round-trip is reported as such, not as ``{}``."""
        profile = self._latency
        if profile is None:
            return {
                "latency_profile_id": "instant_rtt",
                "model_applied": False,
                "place_ns": 0,
                "cancel_ns": 0,
                "submit_ack_latency_ms": 0.0,
                "cancel_ack_latency_ms": 0.0,
            }
        return {
            "latency_profile_id": profile.profile_id or "unlabelled",
            "model_applied": bool(profile.place_ns or profile.cancel_ns),
            "place_ns": int(profile.place_ns),
            "cancel_ns": int(profile.cancel_ns),
            "submit_ack_latency_ms": profile.place_ns / 1_000_000,
            "cancel_ack_latency_ms": profile.cancel_ns / 1_000_000,
        }

    def _run_day(
        self,
        strategy: MakerStrategy,
        events: list[TickData],
    ) -> tuple[list[dict], int, int, int]:
        """Run strategy on one day of events.

        When ``self._latency`` is set, PostQuote/CancelQuote actions do not
        take effect immediately — they are queued with an activation timestamp
        and applied once the market clock reaches it. This models broker RTT
        (Shioaji P95 ~800 ms today) and reproduces the adverse-selection
        window where a cancel is in flight but a trade still fills the order.

        Slice B Task 3 return shape: ``(fills, position, last_mid, last_avg_entry)``.
        ``last_mid`` is the scaled-int mid from the final well-formed bidask
        of the day; ``last_avg_entry`` is the volume-weighted entry price of
        the FIFO residual at end of day (or ``last_mid`` if flat).
        """
        buy_order: QueuePosition | None = None
        sell_order: QueuePosition | None = None
        position = 0
        fills: list[dict] = []
        cur_bid = cur_ask = cur_bid_v = cur_ask_v = 0
        # Slice B Task 3: track last well-formed mid (scaled int) for MtM.
        last_mid_scaled: int = 0

        # D5 pending-action queues. Each entry is (activation_ts, op, payload).
        # op ∈ {"place_buy", "place_sell", "cancel_buy", "cancel_sell"}.
        pending: list[tuple[int, str, QueuePosition | None]] = []
        place_ns = self._latency.place_ns if self._latency else 0
        cancel_ns = self._latency.cancel_ns if self._latency else 0

        def _apply_pending(now_ts: int) -> None:
            """Drain pending actions with activation_ts <= now_ts."""
            nonlocal buy_order, sell_order, pending
            remaining: list[tuple[int, str, QueuePosition | None]] = []
            for ts, op, payload in pending:
                if ts <= now_ts:
                    if op == "place_buy":
                        buy_order = payload
                    elif op == "place_sell":
                        sell_order = payload
                    elif op == "cancel_buy":
                        buy_order = None
                    elif op == "cancel_sell":
                        sell_order = None
                else:
                    remaining.append((ts, op, payload))
            pending = remaining

        for event in events:
            _apply_pending(event.exch_ts)  # D5: drain latency queue first

            if not event.is_trade:
                cur_bid = event.bid_price
                cur_ask = event.ask_price
                cur_bid_v = event.bid_qty
                cur_ask_v = event.ask_qty

                if cur_ask <= cur_bid:
                    continue

                # Slice B Task 3: capture the last well-formed mid (scaled
                # int) for residual MtM. Crossed/locked books are skipped
                # above, so this only updates from sane snapshots.
                last_mid_scaled = (cur_bid + cur_ask) // 2

                if buy_order is not None and buy_order.price != cur_bid:
                    buy_order = None
                if sell_order is not None and sell_order.price != cur_ask:
                    sell_order = None

                actions = strategy.on_tick(event)
                for action in actions:
                    if isinstance(action, PostQuote):
                        qp = self._fill_model.post_quote(
                            action.side,
                            action.price,
                            cur_bid_v if action.side == "buy" else cur_ask_v,
                        )
                        # D5: schedule placement at event.exch_ts + place_ns.
                        # With place_ns=0 (default), this is applied this tick.
                        op = "place_buy" if action.side == "buy" else "place_sell"
                        if place_ns == 0:
                            if action.side == "buy":
                                buy_order = qp
                            else:
                                sell_order = qp
                        else:
                            pending.append((event.exch_ts + place_ns, op, qp))
                    elif isinstance(action, CancelQuote):
                        op = "cancel_buy" if action.side == "buy" else "cancel_sell"
                        if cancel_ns == 0:
                            if action.side == "buy":
                                buy_order = None
                            else:
                                sell_order = None
                        else:
                            pending.append((event.exch_ts + cancel_ns, op, None))
            else:
                mid = (cur_bid + cur_ask) / (2 * event.scale) if cur_bid > 0 else 0

                if buy_order is not None:
                    result = self._fill_model.check_fills(
                        [buy_order],
                        event.trade_price,
                        event.trade_volume,
                    )
                    if result:
                        fills.append(
                            {
                                "side": "buy",
                                "price": buy_order.price,
                                "mid": mid,
                                "spread_pts": ((cur_ask - cur_bid) // event.scale if cur_bid > 0 else 0),
                            }
                        )
                        strategy.on_fill("buy", buy_order.price, mid)
                        position += 1
                        buy_order = None

                if sell_order is not None:
                    result = self._fill_model.check_fills(
                        [sell_order],
                        event.trade_price,
                        event.trade_volume,
                    )
                    if result:
                        fills.append(
                            {
                                "side": "sell",
                                "price": sell_order.price,
                                "mid": mid,
                                "spread_pts": ((cur_ask - cur_bid) // event.scale if cur_bid > 0 else 0),
                            }
                        )
                        strategy.on_fill("sell", sell_order.price, mid)
                        position -= 1
                        sell_order = None

        # Slice B Task 3: derive the FIFO-residual avg entry. If flat, use
        # last_mid so residual_mtm == 0 (matches helper's open_pos==0 path).
        if position == 0:
            last_avg_entry = last_mid_scaled
        else:
            last_avg_entry = self._compute_residual_avg_entry(fills)

        return fills, position, last_mid_scaled, last_avg_entry

    @staticmethod
    def _compute_residual_mtm(
        open_pos: int,
        mark_price: int,
        avg_entry_price: int,
        mark_method: str = "last_mid",
        scale: int = 1_000_000,
    ) -> float:
        """Mark-to-market the un-FIFO'd residual position to a chosen mark.

        Slice B Task 2 - pure static helper. Caller-picks-mark design: the
        helper itself is mark-agnostic; ``mark_method`` is currently advisory
        (recorded for downstream metadata in Task 4) and does not affect the
        arithmetic. The caller resolves whichever mark it wants to use
        (last_mid, last_trade, worse_of_mid_last_trade, ...) and passes the
        resulting scaled-int price as ``mark_price``.

        Args:
            open_pos: Residual position. > 0 long, < 0 short, 0 flat.
            mark_price: Mark price as scaled int (default scale x1_000_000,
                matching ``_compute_fifo_pnl`` and the golden parquet source).
            avg_entry_price: Average entry of the residual position, same
                scale as ``mark_price``.
            mark_method: Advisory string describing how the caller chose the
                mark. Persisted into Task 4's BacktestResult metadata.
            scale: Scaled-int divisor. Defaults to 1_000_000 to match
                ``MakerEngine._compute_fifo_pnl`` and the engine's data path.
                Pass 10_000 if working with the platform-wide CLAUDE.md
                convention (Decimal-style scaled int).

        Returns:
            PnL in points (float). 0.0 when ``open_pos == 0``.
        """
        if open_pos == 0:
            return 0.0
        pnl_int = open_pos * (mark_price - avg_entry_price)
        return pnl_int / scale  # scaled-int -> points

    @staticmethod
    def _compute_residual_avg_entry(fills: list[dict]) -> int:
        """Volume-weighted entry price (scaled int) of the FIFO residual.

        Walks the same matching logic as ``_compute_fifo_pnl`` but instead of
        realizing PnL, returns the mean of whichever side's queue is non-empty
        at end of walk (the un-matched residual). Returns 0 when both queues
        are empty (flat residual — caller's open_pos==0 short-circuit applies).

        We do NOT reuse ``_compute_fifo_pnl`` directly because that helper
        discards the residual queue contents during matching; surfacing them
        would require either (a) refactoring its return signature
        (cross-cutting, would touch all callers and change a frozen helper)
        or (b) a parallel walker. We chose (b) for locality and to keep
        ``_compute_fifo_pnl``'s contract stable.
        """
        buy_q: list[int] = []
        sell_q: list[int] = []

        for f in fills:
            price_scaled = int(f["price"])
            if f["side"] == "buy":
                if sell_q:
                    sell_q.pop(0)
                else:
                    buy_q.append(price_scaled)
            else:
                if buy_q:
                    buy_q.pop(0)
                else:
                    sell_q.append(price_scaled)

        residual = buy_q if buy_q else sell_q
        if not residual:
            return 0
        # Integer mean — matches scaled-int convention; trailing fraction
        # below 1 unit is irrelevant at scale=1_000_000 (sub-microtick).
        return sum(residual) // len(residual)

    @staticmethod
    def _compute_fifo_pnl(fills: list[dict]) -> tuple[float, int, int]:
        """FIFO PnL matching. Returns (gross_pnl_pts, n_round_trips, n_wins)."""
        buy_q: list[float] = []
        sell_q: list[float] = []
        realized = 0.0
        trips = 0
        wins = 0
        scale = 1_000_000

        for f in fills:
            price_pts = f["price"] / scale
            if f["side"] == "buy":
                if sell_q:
                    sp = sell_q.pop(0)
                    pnl = sp - price_pts
                    realized += pnl
                    trips += 1
                    if pnl > 0:
                        wins += 1
                else:
                    buy_q.append(price_pts)
            else:
                if buy_q:
                    bp = buy_q.pop(0)
                    pnl = price_pts - bp
                    realized += pnl
                    trips += 1
                    if pnl > 0:
                        wins += 1
                else:
                    sell_q.append(price_pts)

        return realized, trips, wins


class SimpleMakerStrategy:
    """Generic spread-gated maker strategy for backtest.

    Posts symmetric quotes at best bid/ask when spread >= threshold.
    Manages position within max_pos bounds.
    Compatible with MakerStrategy protocol (on_tick / on_fill).
    """

    __slots__ = ("_spread_threshold", "_max_pos", "_position")

    def __init__(self, spread_threshold_pts: int = 5, max_pos: int = 1) -> None:
        self._spread_threshold = spread_threshold_pts
        self._max_pos = max_pos
        self._position = 0

    def on_tick(self, tick: TickData) -> list[PostQuote | CancelQuote | Hold]:
        if tick.is_trade:
            return [Hold()]
        if tick.spread_pts < self._spread_threshold:
            return [Hold()]
        actions: list[PostQuote | CancelQuote | Hold] = []
        if self._position < self._max_pos:
            actions.append(PostQuote(side="buy", price=tick.bid_price, qty=1))
        if self._position > -self._max_pos:
            actions.append(PostQuote(side="sell", price=tick.ask_price, qty=1))
        return actions or [Hold()]

    def on_fill(self, side: str, price: int, mid_price: float) -> None:
        if side == "buy":
            self._position += 1
        else:
            self._position -= 1
