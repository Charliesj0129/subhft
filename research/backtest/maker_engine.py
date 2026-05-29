"""MakerEngine — CK-direct queue depletion backtest for maker strategies.

Extracted and generalized from research/tools/r47_ck_direct_backtest_v2.py.
Strategy logic is injected via MakerStrategy protocol — engine handles
market simulation, fill determination, and PnL accounting.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import numpy as np
import requests
import structlog

from research.backtest.cost_models import CostModel
from research.backtest.fill_models import FillModel, QueuePosition
from research.backtest.types import BacktestResult

logger = structlog.get_logger()


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


@dataclass(frozen=True)
class PostQuote:
    side: str
    price: int
    qty: int = 1


@dataclass(frozen=True)
class CancelQuote:
    side: str


@dataclass(frozen=True)
class Hold:
    pass


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

    @classmethod
    def shioaji_p95(cls) -> "LatencyProfile":
        """Shioaji sim API P95 RTT (800 ms) applied symmetrically."""
        return cls(place_ns=800_000_000, cancel_ns=800_000_000)


class MakerStrategy(Protocol):
    """Strategy decides when/where to quote. Engine decides fills."""

    def on_tick(self, tick: TickData) -> list[PostQuote | CancelQuote | Hold]: ...

    def on_fill(self, side: str, price: int, mid_price: float) -> None: ...


class ClickHouseSource:
    """Fetch tick + bidask data from ClickHouse."""

    __slots__ = ("_host", "_port", "_password", "_url")

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        password: str | None = None,
    ) -> None:
        self._host = host or os.environ.get("CLICKHOUSE_HOST", "localhost")
        self._port = port or int(os.environ.get("CLICKHOUSE_PORT", "8123"))
        self._password = password or os.environ.get("CLICKHOUSE_PASSWORD", "")
        self._url = f"http://{self._host}:{self._port}/"

    def health_check(self) -> None:
        try:
            resp = requests.post(
                self._url,
                params={"password": self._password},
                data="SELECT 1",
                timeout=5,
            )
            resp.raise_for_status()
        except Exception as exc:
            raise ConnectionError(
                f"ClickHouse not reachable at {self._url}. "
                f"Please start it: docker compose up -d clickhouse\n"
                f"Original error: {exc}"
            ) from exc

    def _query(self, sql: str) -> list[list[str]]:
        resp = requests.post(
            self._url,
            params={"password": self._password},
            data=sql + " FORMAT TSVWithNames",
            timeout=120,
        )
        resp.raise_for_status()
        lines = resp.text.strip().split("\n")
        if len(lines) < 2:
            return []
        rows = [line.split("\t") for line in lines[1:] if line]
        return [lines[0].split("\t")] + rows

    def load_day(self, symbol: str, date: str) -> list[TickData]:
        """Load interleaved bidask + tick events for one day, sorted by exch_ts."""
        scale = 1_000_000

        ba_sql = f"""
        SELECT exch_ts,
               bids_price[1] AS bid1_p, bids_vol[1] AS bid1_v,
               asks_price[1] AS ask1_p, asks_vol[1] AS ask1_v
        FROM hft.market_data
        WHERE symbol = '{symbol}' AND type = 'BidAsk'
          AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
          AND length(bids_price) >= 1 AND length(asks_price) >= 1
        ORDER BY exch_ts
        """
        tick_sql = f"""
        SELECT exch_ts, price_scaled AS price, volume
        FROM hft.market_data
        WHERE symbol = '{symbol}' AND type = 'Tick'
          AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
        ORDER BY exch_ts
        """
        ba_rows = self._query(ba_sql)
        tick_rows = self._query(tick_sql)

        events: list[TickData] = []

        if len(ba_rows) > 1:
            for row in ba_rows[1:]:
                events.append(
                    TickData(
                        exch_ts=int(row[0]),
                        bid_price=int(row[1]),
                        ask_price=int(row[3]),
                        bid_qty=int(row[2]),
                        ask_qty=int(row[4]),
                        trade_price=0,
                        trade_volume=0,
                        is_trade=False,
                        scale=scale,
                    )
                )

        if len(tick_rows) > 1:
            for row in tick_rows[1:]:
                events.append(
                    TickData(
                        exch_ts=int(row[0]),
                        bid_price=0,
                        ask_price=0,
                        bid_qty=0,
                        ask_qty=0,
                        trade_price=int(row[1]),
                        trade_volume=int(row[2]),
                        is_trade=True,
                        scale=scale,
                    )
                )

        events.sort(key=lambda e: e.exch_ts)
        return events

    def available_dates(self, symbol: str) -> list[str]:
        sql = f"""
        SELECT DISTINCT toDate(fromUnixTimestamp64Nano(exch_ts)) AS d
        FROM hft.market_data
        WHERE symbol = '{symbol}'
        ORDER BY d
        """
        rows = self._query(sql)
        if len(rows) <= 1:
            return []
        return [row[0] for row in rows[1:]]


class MakerEngine:
    """CK-direct maker backtest engine."""

    __slots__ = (
        "_fill_model",
        "_cost_model",
        "_ck_source",
        "_latency",
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
    ) -> None:
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

        for date in dates:
            events = self._ck_source.load_day(instrument, date)
            if not events:
                continue

            day_fills, day_position, day_last_mid, day_last_avg = self._run_day(strategy, events)
            day_gross, day_trips, day_wins = self._compute_fifo_pnl(day_fills)

            # Slice B Task 3: residual MtM folded into day-level accounting.
            day_residual_mtm = self._compute_residual_mtm(
                open_pos=day_position,
                mark_price=day_last_mid,
                avg_entry_price=day_last_avg,
                mark_method=self._mark_method,
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

            for f in day_fills:
                spr = f.get("spread_pts", 0)
                if spr not in spread_breakdown:
                    spread_breakdown[spr] = {"fills": 0, "gross_pnl": 0.0}
                spread_breakdown[spr]["fills"] += 1

        equity = np.array(equity_points)
        total_net = self._cost_model.apply(total_gross, total_fills)
        n_days = len(daily_pnl)
        winning_days = sum(1 for d in daily_pnl if d["pnl_pts"] > 0)
        daily_returns = np.diff(equity)

        sharpe = 0.0
        if len(daily_returns) > 1 and np.std(daily_returns) > 0:
            sharpe = float(np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(252))

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
            sharpe_oos=0.0,
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
            latency_profile={},
            engine_type="maker",
            fill_model=self._fill_model.label,
            cost_model=self._cost_model.label,
            instrument=instrument,
            data_period=f"{dates[0]}..{dates[-1]}" if dates else "",
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
            },
            per_spread_breakdown={str(k): v for k, v in sorted(spread_breakdown.items())},
            # Slice B Task 4 + 2026-05-29 punch list: residual decomposition.
            # ``residual_qty`` carries the signed final-day position; the
            # unsigned magnitude is exposed via ``abs_residual_qty``.
            residual_mtm_pts=round(total_residual_mtm, 2),
            residual_qty=final_residual_qty,
            abs_residual_qty=abs(final_residual_qty),
            mark_method=self._mark_method,
            daily_pnl=daily_pnl,
        )

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
