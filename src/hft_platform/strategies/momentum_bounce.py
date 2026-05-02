"""Momentum Bounce Strategy — CBS-flipped with trailing stop exit.

Reuses CascadeBounceStrategy's sigma-normalized large-move detection but
trades WITH the move (momentum) instead of against it (contrarian).

Exit: hybrid trailing stop + hard stop-loss + time cap.
  - Hard SL: exit if PnL <= -(stop_loss_pts)
  - Trailing: exit if PnL drops trailing_stop_pts below peak (peak must be > 0)
  - Time: exit if elapsed >= max_hold_ns

Entry: IOC at best bid/ask (same as CBS).

Allocator Law : Reuses CBS pre-allocated buffers. New state is O(1) per symbol.
Precision Law : All PnL/price arithmetic in scaled int (x10000).
"""

from __future__ import annotations

from structlog import get_logger

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import Side
from hft_platform.events import LOBStatsEvent
from hft_platform.strategies.cascade_bounce import (
    _DEFAULT_LOOKBACK_NS,
    _DEFAULT_MAX_SPREAD_PTS,
    _DEFAULT_MIN_VOL_SAMPLES,
    _DEFAULT_SESSION_END_SEC,
    _DEFAULT_SESSION_START_SEC,
    _DEFAULT_TAKE_PROFIT_PTS,
    _DEFAULT_TRIGGER_SIGMA,
    _MID_X2_POINT_SCALE,
    _PTS_SCALE,
    _UTC_OFFSET_SEC,
    CascadeBounceStrategy,
)

logger = get_logger("strategy.momentum_bounce")

_DEFAULT_TRAILING_STOP_PTS = 6
_DEFAULT_STOP_LOSS_PTS = 10
_DEFAULT_MAX_HOLD_NS = 900_000_000_000  # 15 min


class MomentumBounceStrategy(CascadeBounceStrategy):
    """Momentum strategy: trade WITH sigma-normalized large moves."""

    __slots__ = ("_trailing_stop_pts", "_trailing_stop_scaled", "_peak_pnl_scaled")

    def __init__(
        self,
        strategy_id: str = "momentum_bounce",
        trailing_stop_pts: int = _DEFAULT_TRAILING_STOP_PTS,
        stop_loss_pts: int = _DEFAULT_STOP_LOSS_PTS,
        max_hold_ns: int = _DEFAULT_MAX_HOLD_NS,
        # Explicit forwarding of CBS typed params (was: **kwargs: object splat,
        # which let wrong-type values reach CBS.__init__ and raise TypeError at
        # runtime — the strategy never reached handle_event). Keep these in sync
        # with CascadeBounceStrategy.__init__.
        lookback_ns: int = _DEFAULT_LOOKBACK_NS,
        trigger_sigma: float = _DEFAULT_TRIGGER_SIGMA,
        take_profit_pts: int = _DEFAULT_TAKE_PROFIT_PTS,
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
        super().__init__(
            strategy_id=strategy_id,
            lookback_ns=lookback_ns,
            trigger_sigma=trigger_sigma,
            max_hold_ns=max_hold_ns,
            take_profit_pts=take_profit_pts,
            stop_loss_pts=stop_loss_pts,
            min_vol_samples=min_vol_samples,
            session_start_sec=session_start_sec,
            session_end_sec=session_end_sec,
            utc_offset_sec=utc_offset_sec,
            max_spread_pts=max_spread_pts,
            detect_window_ns=detect_window_ns,
            hold_ns=hold_ns,
            move_threshold_bps=move_threshold_bps,
            stop_loss_bps=stop_loss_bps,
            cooldown_ns=cooldown_ns,
            **kwargs,
        )
        self._trailing_stop_pts = int(trailing_stop_pts)
        self._trailing_stop_scaled = int(trailing_stop_pts) * _PTS_SCALE
        self._peak_pnl_scaled: dict[str, int] = {}

    def _init_symbol(self, symbol: str) -> None:
        super()._init_symbol(symbol)
        if symbol not in self._peak_pnl_scaled:
            self._peak_pnl_scaled[symbol] = 0

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
        if abs(diff_x2) < self._trigger_sigma * local_vol_pts * _MID_X2_POINT_SCALE:
            return

        # MOMENTUM: trade WITH the move (flipped from CBS)
        direction = 1 if diff_x2 > 0 else -1
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
        self._peak_pnl_scaled[symbol] = 0

        logger.info(
            "momentum_entry_signal",
            symbol=symbol,
            direction="long" if direction > 0 else "short",
            move_pts=abs(diff_x2) / _MID_X2_POINT_SCALE,
            local_vol_pts=local_vol_pts,
            trigger_sigma=self._trigger_sigma,
        )

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
            self._awaiting_exit_order[symbol] = False
            self._peak_pnl_scaled[symbol] = 0
            # No passive TP — momentum holds until trailing stop or time exit
            self._state[symbol] = "positioned"
            return

        if event.side == exit_side:
            self._complete_round_trip(symbol)

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

        pnl_scaled = self._direction[symbol] * (mark_price - entry_price)
        elapsed_ns = now_ns - self._entry_ts_ns[symbol]

        # Update peak PnL high-water mark
        if pnl_scaled > self._peak_pnl_scaled[symbol]:
            self._peak_pnl_scaled[symbol] = pnl_scaled

        # Exit conditions
        hard_sl = pnl_scaled <= -(self._stop_loss_pts * _PTS_SCALE)
        trailing = (
            self._peak_pnl_scaled[symbol] > 0
            and pnl_scaled <= self._peak_pnl_scaled[symbol] - self._trailing_stop_scaled
        )
        timeout = elapsed_ns >= self._max_hold_ns

        if not (hard_sl or trailing or timeout):
            return

        exit_reason = "hard_sl" if hard_sl else ("trailing" if trailing else "timeout")
        logger.info(
            "momentum_exit_trigger",
            symbol=symbol,
            reason=exit_reason,
            pnl_pts=pnl_scaled / _PTS_SCALE,
            peak_pts=self._peak_pnl_scaled[symbol] / _PTS_SCALE,
            elapsed_s=elapsed_ns / 1e9,
        )

        # If a passive order is live, cancel it first then IOC exit
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

    def _reset_entry(self, symbol: str) -> None:
        super()._reset_entry(symbol)
        self._peak_pnl_scaled[symbol] = 0

    def _complete_round_trip(self, symbol: str) -> None:
        super()._complete_round_trip(symbol)
        self._peak_pnl_scaled[symbol] = 0
