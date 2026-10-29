"""Taker simulator that charges what the exchange model actually filled (research v2 C2b).

``HftNativeRunner`` runs hftbacktest only as a feed replayer: its bridge sends no orders, so
positions are rebuilt from the signal ladder and valued mid-to-mid with bps fees. This module
keeps the same signal and the same position ladder, but sends each step to the exchange model
as an IOC order at the touch (buy at the ask, sell at the bid) and books the price it filled at.

Method (``METHOD_ID``):

* Signal: ``AlphaStrategyBridge`` unchanged. The target ladder is the one in
  ``hft_native_runner._signals_to_positions``: every signal beyond ``signal_threshold`` moves
  the target one contract, clipped to ``max_position``; the first signal never moves it.
* Orders: at most one order in flight. When none is and the exchange position differs from
  the target, one IOC limit order for the difference goes out at the current touch. Order
  latency is hftbacktest's constant order latency (``latency_us`` each way). An IOC that does
  not fill is dropped; the next signal tries again.
* Fill price: the change in hftbacktest's cash balance divided by the change in position, so
  a fill is charged at the price the exchange model gave it. Exchange fees are off; costs are
  charged afterwards per contract by the statutory cost profile (``cost_models``).
* Days: each input is one trading day and starts flat. ``split_trading_days`` cuts an
  hftbacktest event file into trading days by exchange timestamp: day D is
  ``[previous session 14:00, D 14:00)`` Asia/Taipei on the XTAI calendar (the rule of
  ``config/research/protocols/front_month_roll_v1.yaml`` and the governed reader), so a night
  session belongs to the next trading day. A position still open after the last
  event is closed at that event's touch (a long sells at the bid, a short buys at the ask) and
  those contracts are costed too. Days never share state.
* Sharpe: the daily net PnL series, ``mean / std * sqrt(252)`` (population std), the same
  formula ``MakerEngine`` uses; 0.0 with fewer than two days or zero dispersion.

Prices are in index points: research ``.npy`` rows already carry float points (the exporter
divides ClickHouse x1,000,000 integers), and hftbacktest's balance is in the same unit.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hft_platform.alpha.stats import sharpe_ratio
from hft_platform.contracts.strategy import TIF, OrderIntent
from hft_platform.events import LOBStatsEvent
from research.backtest.alpha_strategy_bridge import _PRICE_SCALE, AlphaStrategyBridge
from research.backtest.cost_models import CostModel

METHOD_ID = "taker_ioc_touch_v1"
ANNUALIZATION_DAYS = 252

# hftbacktest order status / request codes (hftbacktest.order).
_STATUS_NEW = 1
_REQ_NONE = 0


class TakerSimError(RuntimeError):
    """The simulation could not produce a trustworthy result."""


@dataclass(frozen=True, slots=True)
class TakerFill:
    ts_ns: int
    qty: int  # signed: + bought, - sold
    price_pts: float


@dataclass(frozen=True, slots=True)
class TakerDay:
    trading_day: dt.date
    fills: tuple[TakerFill, ...]
    residual_qty: int  # open position closed at the last touch (signed, before closing)
    close_price_pts: float  # price the residual was closed at; 0.0 when flat
    n_contracts: int  # contracts traded, including the closing ones
    gross_pts: float
    cost_pts: float
    net_pts: float

    def to_row(self) -> dict[str, Any]:
        return {
            "date": self.trading_day.isoformat(),
            "n_fills": len(self.fills),
            "n_contracts": self.n_contracts,
            "gross_pts": self.gross_pts,
            "cost_pts": self.cost_pts,
            "net_pts": self.net_pts,
            "residual_qty": self.residual_qty,
            "close_price_pts": self.close_price_pts,
            # Sub-gate row shape (``hft_platform.alpha._sub_gates``): net PnL, fills including
            # the closing one, and no residual inventory (it was closed at the last touch).
            "pnl_pts": self.net_pts,
            "fills": len(self.fills) + (1 if self.residual_qty else 0),
            "residual_mtm_pts": 0.0,
        }


def daily_sharpe(net_pts: Sequence[float]) -> float:
    return sharpe_ratio(net_pts, annualization=ANNUALIZATION_DAYS)


@dataclass(frozen=True)
class TakerSimResult:
    instrument: str
    cost_label: str
    latency_us: int
    queue_model: str
    days: tuple[TakerDay, ...]
    method: str = METHOD_ID

    @property
    def daily_net_pts(self) -> np.ndarray:
        return np.asarray([d.net_pts for d in self.days], dtype=np.float64)

    @property
    def total_net_pts(self) -> float:
        return float(sum(d.net_pts for d in self.days))

    @property
    def sharpe(self) -> float:
        return daily_sharpe([d.net_pts for d in self.days])

    def daily_rows(self) -> list[dict[str, Any]]:
        return [d.to_row() for d in self.days]

    def n_is_days(self, is_oos_split: float) -> int:
        """Leading days that are in-sample: ``int(n * is_oos_split)``, leaving at least one day
        on each side (0 with fewer than two days, so everything is out-of-sample)."""
        n = len(self.days)
        if n < 2:
            return 0
        return max(1, min(n - 1, int(n * float(is_oos_split))))

    def sharpe_split(self, is_oos_split: float) -> tuple[float, float]:
        """``(in-sample, out-of-sample)`` daily Sharpe over the day split of ``n_is_days``."""
        net = [d.net_pts for d in self.days]
        k = self.n_is_days(is_oos_split)
        return daily_sharpe(net[:k]), daily_sharpe(net[k:])


class _TakerOrderBridge(AlphaStrategyBridge):
    """The alpha bridge, plus IOC orders that walk the exchange position toward the ladder."""

    def __init__(self, alpha: Any, *, max_position: int, signal_threshold: float, symbol: str, price_scale: int):
        super().__init__(
            alpha,
            max_position=max_position,
            signal_threshold=signal_threshold,
            symbol=symbol,
            price_scale=price_scale,
            strategy_id="taker_sim",
        )
        self._hbt: Any = None
        self.target = 0
        self.exch_position = 0
        self.balance = 0.0
        self.fills: list[TakerFill] = []
        self.last_bid_pts = 0.0
        self.last_ask_pts = 0.0
        self._inflight_id: int | None = None
        self._ladder_started = False

    def bind(self, hbt: Any) -> None:
        self._hbt = hbt
        state = hbt.state_values(0)
        self.exch_position = int(round(float(state.position)))
        self.balance = float(state.balance)
        if self.exch_position != 0:
            raise TakerSimError(f"exchange model started with position {self.exch_position}; a day must start flat")

    def _collect_fill(self, ts_ns: int) -> None:
        state = self._hbt.state_values(0)
        position = int(round(float(state.position)))
        balance = float(state.balance)
        delta = position - self.exch_position
        if delta != 0:
            price = (self.balance - balance) / delta
            if not np.isfinite(price) or price <= 0.0:
                cash = balance - self.balance
                raise TakerSimError(f"fill at ts={ts_ns} has no usable price (cash {cash}, qty {delta})")
            self.fills.append(TakerFill(ts_ns=ts_ns, qty=delta, price_pts=float(price)))
        elif balance != self.balance:
            raise TakerSimError(f"cash moved at ts={ts_ns} without a position change; exchange fees must be off")
        self.exch_position, self.balance = position, balance

    def _order_in_flight(self) -> bool:
        if self._inflight_id is None:
            return False
        order = self._hbt.orders(0).get(self._inflight_id)
        if order is not None and (order.req != _REQ_NONE or order.status == _STATUS_NEW):
            return True
        self._inflight_id = None
        self._hbt.clear_inactive_orders(0)
        return False

    def handle_event(self, ctx: Any, event: Any) -> list[OrderIntent]:
        if self._hbt is None:
            raise TakerSimError("bind() the exchange model before running")
        n_signals = len(self._signal_log)
        intents = super().handle_event(ctx, event)
        if not isinstance(event, LOBStatsEvent) or len(self._signal_log) == n_signals:
            return intents
        ts_ns = int(event.ts)
        self._collect_fill(ts_ns)
        self.last_bid_pts = float(event.best_bid) / self._price_scale
        self.last_ask_pts = float(event.best_ask) / self._price_scale

        signal = self._signal_log[-1][1]
        if self._ladder_started:
            if signal > self.signal_threshold:
                self.target = min(self.target + 1, self.max_position)
            elif signal < -self.signal_threshold:
                self.target = max(self.target - 1, -self.max_position)
        self._ladder_started = True

        if self._order_in_flight() or self.target == self.exch_position:
            return intents
        qty = self.target - self.exch_position
        if qty > 0:
            self.buy(self._symbol, int(event.best_ask), qty, tif=TIF.IOC)
        else:
            self.sell(self._symbol, int(event.best_bid), -qty, tif=TIF.IOC)
        if self._generated_intents:
            self._inflight_id = int(self._generated_intents[-1].intent_id)
        return self._generated_intents


def _close_day(bridge: _TakerOrderBridge, trading_day: dt.date, cost_model: CostModel) -> TakerDay:
    fills = tuple(bridge.fills)
    residual = bridge.exch_position
    if residual > 0:
        close_price = bridge.last_bid_pts
    elif residual < 0:
        close_price = bridge.last_ask_pts
    else:
        close_price = 0.0
    if residual != 0 and close_price <= 0.0:
        raise TakerSimError(f"{trading_day}: open position {residual} but no touch to close it at")
    cash = -sum(f.qty * f.price_pts for f in fills)
    gross = cash + residual * close_price
    n_contracts = sum(abs(f.qty) for f in fills) + abs(residual)
    net = float(cost_model.apply(gross, n_contracts))
    return TakerDay(
        trading_day=trading_day,
        fills=fills,
        residual_qty=residual,
        close_price_pts=close_price,
        n_contracts=n_contracts,
        gross_pts=float(gross),
        cost_pts=float(gross - net),
        net_pts=net,
    )


@dataclass
class TakerSim:
    """Run one alpha over trading days, one hftbacktest event file per day."""

    alpha: Any
    instrument: str
    cost_model: CostModel
    latency_us: int
    signal_threshold: float = 0.3
    max_position: int = 5
    queue_model: str = "PowerProbQueueModel(3.0)"
    exchange_model: str = "NoPartialFillExchange"
    symbol: str = "ASSET"
    price_scale: int = _PRICE_SCALE
    adapter_kwargs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.latency_us < 0:
            raise ValueError("latency_us must be >= 0")
        if self.max_position < 1:
            raise ValueError("max_position must be >= 1")

    def run_day(self, trading_day: dt.date, event_path: str) -> TakerDay:
        from hft_platform.backtest.adapter import HftBacktestAdapter  # noqa: PLC0415 - needs hftbacktest

        bridge = _TakerOrderBridge(
            self.alpha,
            max_position=self.max_position,
            signal_threshold=self.signal_threshold,
            symbol=self.symbol,
            price_scale=self.price_scale,
        )
        bridge.reset()
        adapter = HftBacktestAdapter(
            strategy=bridge,
            asset_symbol=self.symbol,
            data=event_path,
            latency_us=int(self.latency_us),
            price_scale=self.price_scale,
            equity_sample_ns=0,
            maker_fee=0.0,
            taker_fee=0.0,
            queue_model=self.queue_model,
            exchange_model=self.exchange_model,
            **self.adapter_kwargs,
        )
        bridge.bind(adapter.hbt)
        adapter.run()
        if not bridge.signal_log:
            raise TakerSimError(f"{trading_day}: no book events in {event_path}")
        return _close_day(bridge, trading_day, self.cost_model)

    def run(self, days: Sequence[tuple[dt.date, str]]) -> TakerSimResult:
        """``days`` pairs each trading day with its hftbacktest event file (``ensure_hftbt_npz``)."""
        if not days:
            raise TakerSimError("no trading days to simulate")
        seen = [day for day, _ in days]
        if len(set(seen)) != len(seen):
            raise TakerSimError("a trading day appears more than once")
        if seen != sorted(seen):
            raise TakerSimError("trading days must be in order")
        results = tuple(self.run_day(day, path) for day, path in days)
        return TakerSimResult(
            instrument=self.instrument,
            cost_label=self.cost_model.label,
            latency_us=int(self.latency_us),
            queue_model=self.queue_model,
            days=results,
        )


def split_trading_days(
    hbt_path: str, out_dir: str, *, calendar: Any = None, protocol: Any = None
) -> list[tuple[dt.date, str]]:
    """Write one event file per trading day of ``hbt_path`` into ``out_dir``, in day order.

    Fails closed: unordered exchange timestamps, a date outside the calendar, or no events.
    """
    from research.backtest import rolled_series

    calendar = calendar or rolled_series.SessionCalendar("XTAI")
    protocol = protocol or rolled_series.load_roll_protocol()
    with np.load(hbt_path, allow_pickle=False) as npz:
        events = np.asarray(npz["data"])
    if events.size == 0:
        raise TakerSimError(f"{hbt_path}: no events")
    ts = events["exch_ts"].astype(np.int64)
    if np.any(np.diff(ts) < 0):
        raise TakerSimError(f"{hbt_path}: exchange timestamps are not in order")

    first = dt.datetime.fromtimestamp(int(ts[0]) // 1_000_000_000, tz=protocol.tz)
    last = dt.datetime.fromtimestamp(int(ts[-1]) // 1_000_000_000, tz=protocol.tz)
    offset = first.utcoffset()
    if offset is None or last.utcoffset() != offset:
        raise TakerSimError(f"{hbt_path}: {protocol.tz.key} changes UTC offset inside the file")
    local_s = ts // 1_000_000_000 + int(offset.total_seconds())
    local_day = local_s // 86_400
    split_s = protocol.window_start.hour * 3600 + protocol.window_start.minute * 60
    late = (local_s % 86_400) >= split_s
    slot = local_day * 2 + late.astype(np.int64)

    epoch = dt.date(1970, 1, 1)
    trading_day_of_slot: dict[int, dt.date] = {}
    for value in np.unique(slot).tolist():
        day = epoch + dt.timedelta(days=value // 2)
        try:
            on_session = not value % 2 and calendar.is_session(day)
            trading_day_of_slot[value] = day if on_session else calendar.next_session(day)
        except rolled_series.RollProtocolError as exc:
            raise TakerSimError(f"{hbt_path}: {exc}") from exc

    day_of_row = np.asarray([trading_day_of_slot[v].toordinal() for v in slot.tolist()], dtype=np.int64)
    stem = os.path.basename(hbt_path).removesuffix(".npz")
    out: list[tuple[dt.date, str]] = []
    for ordinal in np.unique(day_of_row).tolist():
        day = dt.date.fromordinal(ordinal)
        path = os.path.join(out_dir, f"{stem}.td{day:%Y%m%d}.npz")
        np.savez_compressed(path, data=events[day_of_row == ordinal])
        out.append((day, path))
    return out


def simulate_files(sim: TakerSim, hbt_paths: Sequence[str]) -> TakerSimResult:
    """Run ``sim`` over every trading day of ``hbt_paths``; a day in two files is an error."""
    if not hbt_paths:
        raise TakerSimError("no event files to simulate")
    tmp_dir = tempfile.mkdtemp(prefix="takersim_")
    try:
        days: list[tuple[dt.date, str]] = []
        for path in hbt_paths:
            days.extend(split_trading_days(path, tmp_dir))
        days.sort(key=lambda item: item[0])
        return sim.run(days)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
