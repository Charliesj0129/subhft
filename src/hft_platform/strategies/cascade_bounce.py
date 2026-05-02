"""Cascade Bounce Strategy with normalized triggers and passive exits."""

from __future__ import annotations

import math
from collections import deque

from structlog import get_logger

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus
from hft_platform.contracts.strategy import TIF, Side
from hft_platform.core import timebase
from hft_platform.events import LOBStatsEvent
from hft_platform.strategy.base import BaseStrategy

logger = get_logger("strategy.cbs")

_DEFAULT_LOOKBACK_NS = 300_000_000_000
_DEFAULT_TRIGGER_SIGMA = 3.0
_DEFAULT_MAX_HOLD_NS = 300_000_000_000
_DEFAULT_TAKE_PROFIT_PTS = 8
_DEFAULT_STOP_LOSS_PTS = 6
_DEFAULT_MIN_VOL_SAMPLES = 8
_DEFAULT_SESSION_START_SEC = 9 * 3600 + 15 * 60
_DEFAULT_SESSION_END_SEC = 13 * 3600 + 35 * 60
_DEFAULT_MAX_SPREAD_PTS = 3
_UTC_OFFSET_SEC = 8 * 3600
_PTS_SCALE = 10_000
_MID_X2_POINT_SCALE = 20_000
_TERMINAL_ORDER_STATUSES = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.FAILED}


class _PriceEntry:
    __slots__ = ("ts_ns", "mid_x2")

    def __init__(self, ts_ns: int, mid_x2: int) -> None:
        self.ts_ns = ts_ns
        self.mid_x2 = mid_x2


class CascadeBounceStrategy(BaseStrategy):
    """Contrarian strategy using vol-normalized entry and passive take-profit exits."""

    __slots__ = (
        "_lookback_ns",
        "_trigger_sigma",
        "_max_hold_ns",
        "_take_profit_pts",
        "_stop_loss_pts",
        "_min_vol_samples",
        "_session_start_sec",
        "_session_end_sec",
        "_utc_offset_sec",
        "_price_buf",
        "_state",
        "_entry_ts_ns",
        "_entry_price",
        "_direction",
        "_next_allowed_ts",
        "_awaiting_exit_order",
        "_exit_order_id",
        "_pending_force_close",
        "_aggressive_exit_inflight",
        "_max_spread_scaled",
    )

    def __init__(
        self,
        strategy_id: str = "cascade_bounce",
        lookback_ns: int = _DEFAULT_LOOKBACK_NS,
        trigger_sigma: float = _DEFAULT_TRIGGER_SIGMA,
        max_hold_ns: int = _DEFAULT_MAX_HOLD_NS,
        take_profit_pts: int = _DEFAULT_TAKE_PROFIT_PTS,
        stop_loss_pts: int = _DEFAULT_STOP_LOSS_PTS,
        min_vol_samples: int = _DEFAULT_MIN_VOL_SAMPLES,
        session_start_sec: int = _DEFAULT_SESSION_START_SEC,
        session_end_sec: int = _DEFAULT_SESSION_END_SEC,
        utc_offset_sec: int = _UTC_OFFSET_SEC,
        max_spread_pts: int = _DEFAULT_MAX_SPREAD_PTS,
        detect_window_ns: int | None = None,
        hold_ns: int | None = None,
        move_threshold_bps: int | None = None,
        stop_loss_bps: int | None = None,
        cooldown_ns: int | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(strategy_id=strategy_id, **kwargs)
        self._lookback_ns = int(detect_window_ns or lookback_ns)
        self._trigger_sigma = float(trigger_sigma if move_threshold_bps is None else trigger_sigma)
        self._max_hold_ns = int(hold_ns or max_hold_ns)
        self._take_profit_pts = int(take_profit_pts)
        self._stop_loss_pts = int(stop_loss_bps or stop_loss_pts)
        self._min_vol_samples = int(min_vol_samples)
        self._session_start_sec = int(session_start_sec)
        self._session_end_sec = int(session_end_sec)
        self._utc_offset_sec = int(utc_offset_sec)
        self._max_spread_scaled = int(max_spread_pts) * _PTS_SCALE

        self._price_buf: dict[str, deque[_PriceEntry]] = {}
        self._state: dict[str, str] = {}
        self._entry_ts_ns: dict[str, int] = {}
        self._entry_price: dict[str, int] = {}
        self._direction: dict[str, int] = {}
        self._next_allowed_ts: dict[str, int] = {}
        self._awaiting_exit_order: dict[str, bool] = {}
        self._exit_order_id: dict[str, str] = {}
        self._pending_force_close: dict[str, bool] = {}
        self._aggressive_exit_inflight: dict[str, bool] = {}

    def _init_symbol(self, symbol: str) -> None:
        if symbol in self._state:
            return
        self._price_buf[symbol] = deque(maxlen=4096)
        self._state[symbol] = "idle"
        self._entry_ts_ns[symbol] = 0
        self._entry_price[symbol] = 0
        self._direction[symbol] = 0
        self._next_allowed_ts[symbol] = 0
        self._awaiting_exit_order[symbol] = False
        self._exit_order_id[symbol] = ""
        self._pending_force_close[symbol] = False
        self._aggressive_exit_inflight[symbol] = False

    def _in_session(self, now_ns: int) -> bool:
        sec_of_day = ((now_ns // 1_000_000_000) + self._utc_offset_sec) % 86400
        start = self._session_start_sec
        end = self._session_end_sec
        if start <= end:
            return start <= sec_of_day <= end
        return sec_of_day >= start or sec_of_day <= end

    def _entry_side(self, symbol: str) -> Side:
        return Side.BUY if self._direction[symbol] > 0 else Side.SELL

    def _exit_side(self, symbol: str) -> Side:
        return Side.SELL if self._direction[symbol] > 0 else Side.BUY

    def _rolling_rms_point_change(self, buf: deque[_PriceEntry]) -> float:
        n = len(buf)
        if n < 2:
            return 0.0
        sum_sq_scaled = 0
        for i in range(1, n):
            delta = abs(buf[i].mid_x2 - buf[i - 1].mid_x2)
            sum_sq_scaled += delta * delta
        # Convert from (mid_x2 units)^2 to (points)^2: divide by _MID_X2_POINT_SCALE^2
        # Then take sqrt and divide by (n-1) for RMS
        # = sqrt(sum_sq_scaled / (n-1)) / _MID_X2_POINT_SCALE
        return math.sqrt(sum_sq_scaled / (n - 1)) / _MID_X2_POINT_SCALE

    def on_stats(self, event: LOBStatsEvent) -> None:
        symbol = event.symbol
        self._init_symbol(symbol)

        mid_x2 = int(event.mid_price_x2 or 0)
        if mid_x2 <= 0:
            return

        now_ns = int(event.ts or timebase.now_ns())
        buf = self._price_buf[symbol]
        cutoff = now_ns - self._lookback_ns
        while buf and buf[0].ts_ns < cutoff:
            buf.popleft()
        buf.append(_PriceEntry(now_ns, mid_x2))

        if self._pending_force_close[symbol] and not self._exit_order_id[symbol]:
            self._emit_aggressive_exit(symbol, event.best_bid, event.best_ask)
            return

        state = self._state[symbol]
        if state == "idle":
            self._check_entry(symbol, now_ns, event)
            return
        if state in {"positioned", "exit_live"}:
            self._check_exit(symbol, now_ns, event)

    def _check_entry(self, symbol: str, now_ns: int, event: LOBStatsEvent) -> None:
        if now_ns < self._next_allowed_ts[symbol]:
            return
        if not self._in_session(now_ns):
            return
        if self.position(symbol) != 0:
            return
        spread = int(event.spread_scaled or 0)
        if spread <= 0 or spread > self._max_spread_scaled:
            return

        buf = self._price_buf[symbol]
        if len(buf) < self._min_vol_samples + 1:
            return

        oldest = buf[0]
        diff_x2 = int(event.mid_price_x2 or 0) - oldest.mid_x2
        local_vol_pts = max(self._rolling_rms_point_change(buf), 1.0)
        # Compare in mid_x2 units: abs(diff_x2) < trigger_sigma * local_vol_pts * _MID_X2_POINT_SCALE
        if abs(diff_x2) < self._trigger_sigma * local_vol_pts * _MID_X2_POINT_SCALE:
            return

        direction = -1 if diff_x2 > 0 else 1
        if direction > 0:
            side = Side.BUY
            aggressive_price = int(event.best_ask or 0)
        else:
            side = Side.SELL
            aggressive_price = int(event.best_bid or 0)
        if aggressive_price <= 0:
            return

        self._place_entry(symbol, side, aggressive_price)
        self._state[symbol] = "awaiting_entry_fill"
        self._direction[symbol] = direction
        self._entry_ts_ns[symbol] = now_ns
        self._entry_price[symbol] = 0
        self._awaiting_exit_order[symbol] = False
        self._exit_order_id[symbol] = ""
        self._pending_force_close[symbol] = False
        self._aggressive_exit_inflight[symbol] = False

        logger.info(
            "cbs_entry_signal",
            symbol=symbol,
            direction="long" if direction > 0 else "short",
            move_pts=abs(diff_x2) / _MID_X2_POINT_SCALE,
            local_vol_pts=local_vol_pts,
            trigger_sigma=self._trigger_sigma,
        )

    def _place_entry(self, symbol: str, side: Side, price: int) -> None:
        if side == Side.BUY:
            self.buy(symbol, price, 1, tif=TIF.IOC)
        else:
            self.sell(symbol, price, 1, tif=TIF.IOC)

    def on_fill(self, event: FillEvent) -> None:
        symbol = event.symbol
        self._init_symbol(symbol)

        direction = self._direction[symbol]
        if direction == 0:
            return

        entry_side = self._entry_side(symbol)
        exit_side = self._exit_side(symbol)

        if self._state[symbol] == "awaiting_entry_fill" and event.side == entry_side:
            self._entry_price[symbol] = int(event.price)
            if int(event.match_ts_ns or 0) > 0:
                self._entry_ts_ns[symbol] = int(event.match_ts_ns)
            self._aggressive_exit_inflight[symbol] = False
            self._pending_force_close[symbol] = False
            self._awaiting_exit_order[symbol] = True
            self._state[symbol] = "exit_live"
            self._place_take_profit(symbol)
            return

        if event.side == exit_side:
            self._complete_round_trip(symbol)

    def _place_take_profit(self, symbol: str) -> None:
        entry_price = self._entry_price[symbol]
        if entry_price <= 0:
            return
        if self._direction[symbol] > 0:
            exit_price = entry_price + (self._take_profit_pts * _PTS_SCALE)
            self.sell(symbol, exit_price, 1, tif=TIF.LIMIT)
        else:
            exit_price = max(_PTS_SCALE, entry_price - (self._take_profit_pts * _PTS_SCALE))
            self.buy(symbol, exit_price, 1, tif=TIF.LIMIT)

    def on_order(self, event: OrderEvent) -> None:
        symbol = event.symbol
        self._init_symbol(symbol)
        if self._direction[symbol] == 0:
            return

        if self._state[symbol] == "awaiting_entry_fill":
            if (
                event.side == self._entry_side(symbol)
                and event.status in _TERMINAL_ORDER_STATUSES
                and event.filled_qty == 0
            ):
                self._reset_entry(symbol)
            return

        if self._awaiting_exit_order[symbol] and event.side == self._exit_side(symbol):
            if event.status in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING_SUBMIT}:
                self._exit_order_id[symbol] = event.order_id
                self._awaiting_exit_order[symbol] = False
                self._state[symbol] = "exit_live"
                return
            if event.status in _TERMINAL_ORDER_STATUSES and event.filled_qty == 0:
                self._aggressive_exit_inflight[symbol] = False
                self._awaiting_exit_order[symbol] = False
                self._pending_force_close[symbol] = True
                return

        if event.order_id != self._exit_order_id[symbol]:
            return

        if event.status == OrderStatus.FILLED:
            self._complete_round_trip(symbol)
            return

        if event.status in _TERMINAL_ORDER_STATUSES:
            self._exit_order_id[symbol] = ""
            self._awaiting_exit_order[symbol] = False
            self._state[symbol] = "positioned"

    def _check_exit(self, symbol: str, now_ns: int, event: LOBStatsEvent) -> None:
        entry_price = self._entry_price[symbol]
        if entry_price <= 0:
            return

        if self._direction[symbol] > 0:
            mark_price = int(event.best_bid or 0)
        else:
            mark_price = int(event.best_ask or 0)
        if mark_price <= 0:
            mark_price = int((event.mid_price_x2 or 0) // 2)
        if mark_price <= 0:
            return

        # pnl in scaled units (x10000): positive = profit, negative = loss
        pnl_scaled = self._direction[symbol] * (mark_price - entry_price)
        elapsed_ns = now_ns - self._entry_ts_ns[symbol]
        should_exit = pnl_scaled <= -(self._stop_loss_pts * _PTS_SCALE) or elapsed_ns >= self._max_hold_ns
        if not should_exit:
            return

        if self._exit_order_id[symbol]:
            self.cancel(symbol, self._exit_order_id[symbol])
            self._pending_force_close[symbol] = True
            self._exit_order_id[symbol] = ""
            self._awaiting_exit_order[symbol] = False
            self._state[symbol] = "positioned"
            return

        if self._awaiting_exit_order[symbol]:
            return

        self._emit_aggressive_exit(symbol, int(event.best_bid or 0), int(event.best_ask or 0))

    def _emit_aggressive_exit(self, symbol: str, best_bid: int, best_ask: int) -> None:
        if self._aggressive_exit_inflight[symbol]:
            return
        exit_side = self._exit_side(symbol)
        if exit_side == Side.SELL:
            price = best_bid
            if price > 0:
                self.sell(symbol, price, 1, tif=TIF.IOC)
        else:
            price = best_ask
            if price > 0:
                self.buy(symbol, price, 1, tif=TIF.IOC)
        if price > 0:
            self._aggressive_exit_inflight[symbol] = True
            self._awaiting_exit_order[symbol] = True
            self._pending_force_close[symbol] = False

    def _reset_entry(self, symbol: str) -> None:
        self._state[symbol] = "idle"
        self._entry_ts_ns[symbol] = 0
        self._entry_price[symbol] = 0
        self._direction[symbol] = 0
        self._awaiting_exit_order[symbol] = False
        self._exit_order_id[symbol] = ""
        self._pending_force_close[symbol] = False
        self._aggressive_exit_inflight[symbol] = False

    def _complete_round_trip(self, symbol: str) -> None:
        self._state[symbol] = "idle"
        self._next_allowed_ts[symbol] = self._entry_ts_ns[symbol] + self._max_hold_ns
        self._entry_price[symbol] = 0
        self._entry_ts_ns[symbol] = 0
        self._direction[symbol] = 0
        self._awaiting_exit_order[symbol] = False
        self._exit_order_id[symbol] = ""
        self._pending_force_close[symbol] = False
        self._aggressive_exit_inflight[symbol] = False
