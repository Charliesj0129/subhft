"""Cascade Bounce Strategy (CBS) — contrarian entry after large price moves.

Detects large intraday price moves (>= threshold bps within a detection window)
and enters contrarian, betting on mean-reversion. Holds for a fixed period with
a tight stop-loss.

Research: Round 14, Alpha Research Team (2026-03-26)
Paper basis: Vlasiuk & Smirnov (2511.06177) — push-response anomalies
Validated: OOS +3.00 bps/trade on TXFD6 L1 data (13 days, 7/10 days profitable)

Economics (TMF / 微台指):
    Contract value: 30,000 × 10 = 300,000 NTD
    RT cost: ~1.33 bps (40 NTD commission)
    Move threshold: 40 bps → contrarian entry
    Hold: 300s, stop-loss: 15 bps
    Rest-of-day only (opening 30 min excluded — directional gap effects)
    Net OOS capture: +3.00 bps/trade (rest-of-day: +3.95 bps/trade)

Key constraints:
    - Non-overlapping: next entry only after entry_ts + hold_period
    - Session gate: 09:15-13:45 (exclude opening 30 min)
    - Single position at a time
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from structlog import get_logger

from hft_platform.contracts.strategy import TIF, Side
from hft_platform.core import timebase
from hft_platform.events import LOBStatsEvent
from hft_platform.execution.execution_optimizer import ExecutionOptimizer, OrderType
from hft_platform.strategy.base import BaseStrategy

logger = get_logger("strategy.cbs")

# Default parameter values (CBS-40-300, validated OOS)
_DEFAULT_MOVE_THRESHOLD_BPS: int = 40
_DEFAULT_DETECT_WINDOW_NS: int = 600_000_000_000  # 600s
_DEFAULT_HOLD_NS: int = 300_000_000_000  # 300s
_DEFAULT_STOP_LOSS_BPS: int = 15
_DEFAULT_COOLDOWN_NS: int = 5_000_000_000  # 5s after detection before entry

# Wall-clock session boundaries (seconds since midnight, local time)
# TMFD6 regular session: 08:45-13:45 TST. Active window: 09:15-13:35.
_DEFAULT_SESSION_START_SEC: int = 9 * 3600 + 15 * 60  # 09:15 = 33300
_DEFAULT_SESSION_END_SEC: int = 13 * 3600 + 35 * 60  # 13:35 = 48900
_UTC_OFFSET_SEC: int = 8 * 3600  # UTC+8 for Asia/Taipei (TAIFEX)


class _PriceEntry:
    """Lightweight price+timestamp for the detection window deque."""

    __slots__ = ("ts_ns", "mid_x2")

    def __init__(self, ts_ns: int, mid_x2: int) -> None:
        self.ts_ns = ts_ns
        self.mid_x2 = mid_x2


class CascadeBounceStrategy(BaseStrategy):
    """Contrarian strategy that enters after large intraday price moves.

    Parameters
    ----------
    strategy_id : str
        Strategy identifier.
    move_threshold_bps : int
        Minimum move size (bps) within detection window to trigger entry.
    detect_window_ns : int
        Lookback window (ns) for move detection.
    hold_ns : int
        Hold period (ns) after entry.
    stop_loss_bps : int
        Maximum adverse move (bps) before stop-loss exit.
    cooldown_ns : int
        Cooldown (ns) after move detection before entry.
    session_start_sec : int
        Earliest wall-clock second-of-day (local) for new entries.
    session_end_sec : int
        Latest wall-clock second-of-day (local) for new entries.
    utc_offset_sec : int
        UTC offset in seconds for exchange timestamps (default: +28800 for Asia/Taipei).
    **kwargs
        Passed through to BaseStrategy.
    """

    __slots__ = (
        "_move_threshold_bps",
        "_detect_window_ns",
        "_hold_ns",
        "_stop_loss_bps",
        "_cooldown_ns",
        "_session_start_sec",
        "_session_end_sec",
        "_utc_offset_sec",
        "_price_buf",
        "_state",
        "_entry_ts_ns",
        "_entry_mid_x2",
        "_direction",
        "_next_allowed_ts",
        "_exec_optimizer",
        "_pending_entry_symbol",
        "_pending_entry_price",
    )

    def __init__(
        self,
        strategy_id: str = "cascade_bounce",
        move_threshold_bps: int = _DEFAULT_MOVE_THRESHOLD_BPS,
        detect_window_ns: int = _DEFAULT_DETECT_WINDOW_NS,
        hold_ns: int = _DEFAULT_HOLD_NS,
        stop_loss_bps: int = _DEFAULT_STOP_LOSS_BPS,
        cooldown_ns: int = _DEFAULT_COOLDOWN_NS,
        session_start_sec: int = _DEFAULT_SESSION_START_SEC,
        session_end_sec: int = _DEFAULT_SESSION_END_SEC,
        utc_offset_sec: int = _UTC_OFFSET_SEC,
        exec_optimizer_enabled: bool = False,
        exec_spread_threshold_pts: int = 2,
        exec_fill_score_threshold: float = 1.5,
        exec_limit_timeout_ns: int = 3_000_000_000,
        **kwargs: object,
    ) -> None:
        super().__init__(strategy_id=strategy_id, **kwargs)
        self._move_threshold_bps: int = move_threshold_bps
        self._detect_window_ns: int = detect_window_ns
        self._hold_ns: int = hold_ns
        self._stop_loss_bps: int = stop_loss_bps
        self._cooldown_ns: int = cooldown_ns
        self._session_start_sec: int = session_start_sec
        self._session_end_sec: int = session_end_sec
        self._utc_offset_sec: int = utc_offset_sec

        # Execution optimizer (limit vs market order decision)
        self._exec_optimizer = ExecutionOptimizer(
            spread_threshold_pts=exec_spread_threshold_pts,
            fill_score_threshold=exec_fill_score_threshold,
            limit_timeout_ns=exec_limit_timeout_ns,
            enabled=exec_optimizer_enabled,
        )
        self._pending_entry_symbol: str = ""
        self._pending_entry_price: int = 0

        # Per-symbol state
        self._price_buf: dict[str, deque[_PriceEntry]] = {}
        self._state: dict[str, str] = {}  # "idle" | "pending_limit" | "positioned"
        self._entry_ts_ns: dict[str, int] = {}
        self._entry_mid_x2: dict[str, int] = {}
        self._direction: dict[str, int] = {}  # +1 = long, -1 = short
        self._next_allowed_ts: dict[str, int] = {}

    def _init_symbol(self, symbol: str) -> None:
        """Lazily initialize per-symbol state."""
        if symbol not in self._state:
            self._price_buf[symbol] = deque(maxlen=8192)
            self._state[symbol] = "idle"
            self._entry_ts_ns[symbol] = 0
            self._entry_mid_x2[symbol] = 0
            self._direction[symbol] = 0
            self._next_allowed_ts[symbol] = 0

    def on_stats(self, event: LOBStatsEvent) -> None:
        """Process LOB stats: maintain price window, detect moves, manage position."""
        symbol = event.symbol
        self._init_symbol(symbol)

        mid_x2 = event.mid_price_x2
        if mid_x2 is None or mid_x2 <= 0:
            return

        now_ns = event.ts
        if now_ns <= 0:
            now_ns = timebase.now_ns()

        # Update price buffer (expire old entries)
        buf = self._price_buf[symbol]
        cutoff = now_ns - self._detect_window_ns
        while buf and buf[0].ts_ns < cutoff:
            buf.popleft()
        buf.append(_PriceEntry(now_ns, mid_x2))

        state = self._state[symbol]

        if state == "positioned":
            self._check_exit(symbol, now_ns, mid_x2)
        elif state == "pending_limit":
            self._check_pending_limit(symbol, now_ns, event)
        elif state == "idle":
            self._check_entry(symbol, now_ns, mid_x2, event)

    def _in_session(self, now_ns: int) -> bool:
        """Check if wall-clock time is within active trading window.

        Converts the UTC epoch timestamp to local second-of-day using
        the configured UTC offset (default: +8h for Asia/Taipei).
        """
        sec_of_day = ((now_ns // 1_000_000_000) + self._utc_offset_sec) % 86400
        return self._session_start_sec <= sec_of_day <= self._session_end_sec

    def _check_entry(
        self,
        symbol: str,
        now_ns: int,
        mid_x2: int,
        event: LOBStatsEvent,
    ) -> None:
        """Detect large moves and enter contrarian."""
        # Enforce non-overlapping cooldown
        if now_ns < self._next_allowed_ts[symbol]:
            return

        # Session gate (wall-clock time)
        if not self._in_session(now_ns):
            return

        # Need sufficient price history
        buf = self._price_buf[symbol]
        if len(buf) < 2:
            return

        # Compute move from window start to current price
        oldest = buf[0]
        if oldest.mid_x2 <= 0:
            return

        # Move in bps (using mid_x2: multiply by 20000 then divide by mid_x2 for bps)
        # move_bps = (mid_x2 - oldest.mid_x2) / oldest.mid_x2 * 10000
        # Using integer math to avoid float: move_bps_x100 for precision
        diff = mid_x2 - oldest.mid_x2
        move_bps_x100 = diff * 1_000_000 // oldest.mid_x2
        move_bps = move_bps_x100 // 100
        abs_move = abs(move_bps)

        if abs_move < self._move_threshold_bps:
            return

        # Large move detected! Enter contrarian
        direction = -1 if diff > 0 else 1  # contrarian: sell if up, buy if down

        # Check we're not already positioned
        pos = self.position(symbol)
        if pos != 0:
            return

        # Determine entry side and prices
        if direction == 1:
            side = Side.BUY
            aggressive_price = event.best_ask  # cross the spread
            passive_price = event.best_bid  # join the bid
        else:
            side = Side.SELL
            aggressive_price = event.best_bid  # cross the spread
            passive_price = event.best_ask  # join the ask

        if aggressive_price <= 0:
            return

        # Execution optimizer: limit vs market decision
        spread_pts = event.spread_scaled // 10000 if event.spread_scaled else 0
        bid_depth = int(event.bid_depth or 0)
        ask_depth = int(event.ask_depth or 0)
        near_depth = bid_depth if direction == 1 else ask_depth
        opp_depth = ask_depth if direction == 1 else bid_depth
        imbalance_ppm = int(
            ((bid_depth - ask_depth) * 1_000_000 // max(bid_depth + ask_depth, 1)) if (bid_depth + ask_depth) > 0 else 0
        )

        order_type = self._exec_optimizer.decide(
            spread_pts=spread_pts,
            near_depth=near_depth,
            opp_depth=opp_depth,
            imbalance_ppm=imbalance_ppm,
            side=direction,
            ts_ns=now_ns,
        )

        if order_type == OrderType.LIMIT and passive_price > 0:
            # Passive limit order — join the queue
            self._place_entry(symbol, side, passive_price, TIF.LIMIT)
            self._state[symbol] = "pending_limit"
            self._pending_entry_symbol = symbol
            self._pending_entry_price = passive_price
        else:
            # Aggressive market order — cross the spread
            self._place_entry(symbol, side, aggressive_price, TIF.IOC)
            self._state[symbol] = "positioned"

        self._entry_ts_ns[symbol] = now_ns
        self._entry_mid_x2[symbol] = mid_x2
        self._direction[symbol] = direction

        logger.info(
            "cbs_entry",
            symbol=symbol,
            direction="long" if direction == 1 else "short",
            order_type="LIMIT" if order_type == OrderType.LIMIT else "MARKET",
            move_bps=move_bps,
            mid_x2=mid_x2,
            spread_pts=spread_pts,
        )

    def _place_entry(self, symbol: str, side: Side, price: int, tif: TIF) -> None:
        """Place an entry order with the specified TIF."""
        if side == Side.BUY:
            self.buy(symbol, price, 1, tif=tif)
        else:
            self.sell(symbol, price, 1, tif=tif)

    def _check_pending_limit(self, symbol: str, now_ns: int, event: LOBStatsEvent) -> None:
        """Check if pending limit entry has timed out."""
        # Check if we got filled (position changed)
        pos = self.position(symbol)
        expected_pos = self._direction[symbol]  # +1 or -1
        if (expected_pos > 0 and pos > 0) or (expected_pos < 0 and pos < 0):
            # Filled — transition to positioned
            self._state[symbol] = "positioned"
            self._exec_optimizer.on_fill()
            logger.info("cbs_limit_filled", symbol=symbol)
            return

        # Check timeout
        if self._exec_optimizer.check_timeout(now_ns):
            # Timeout — cancel limit and switch to aggressive market order
            self._exec_optimizer.on_cancel()
            direction = self._direction[symbol]
            if direction == 1:
                price = int(event.best_ask or 0)
                side = Side.BUY
            else:
                price = int(event.best_bid or 0)
                side = Side.SELL

            if price > 0:
                self._place_entry(symbol, side, price, TIF.IOC)
                self._state[symbol] = "positioned"
                logger.info(
                    "cbs_limit_timeout_market_fallback",
                    symbol=symbol,
                    fallback_price=price,
                )
            else:
                # No valid price — abort entry
                self._state[symbol] = "idle"
                self._direction[symbol] = 0
                logger.warning("cbs_limit_timeout_no_price", symbol=symbol)

    def _check_exit(self, symbol: str, now_ns: int, mid_x2: int) -> None:
        """Check stop-loss and time-based exit conditions."""
        entry_mid = self._entry_mid_x2[symbol]
        entry_ts = self._entry_ts_ns[symbol]
        direction = self._direction[symbol]

        if entry_mid <= 0:
            return

        # Unrealized PnL in bps (integer math)
        pnl_diff = direction * (mid_x2 - entry_mid)
        pnl_bps_x100 = pnl_diff * 1_000_000 // entry_mid
        pnl_bps = pnl_bps_x100 // 100

        elapsed_ns = now_ns - entry_ts
        exit_reason: Optional[str] = None

        # Stop-loss check
        if pnl_bps < -self._stop_loss_bps:
            exit_reason = "stop_loss"

        # Time-based exit
        if elapsed_ns >= self._hold_ns:
            exit_reason = "time_exit"

        if exit_reason is None:
            return

        # Exit: close the position
        if not self.ctx:
            return

        l1 = self.ctx.get_l1_scaled(symbol)
        if l1 is not None:
            # l1 = (ts, bid, ask, mid_x2, spread, bid_depth, ask_depth)
            if direction == 1:
                # Long → sell at best bid
                exit_price = l1[1]  # best_bid
            else:
                # Short → buy at best ask
                exit_price = l1[2]  # best_ask
        else:
            # Fallback: use aggressive exit
            exit_price = mid_x2 // 2  # approximate mid

        if exit_price <= 0:
            return

        exit_side = Side.SELL if direction == 1 else Side.BUY
        if exit_side == Side.BUY:
            self.buy(symbol, exit_price, 1)
        else:
            self.sell(symbol, exit_price, 1)

        logger.info(
            "cbs_exit",
            symbol=symbol,
            reason=exit_reason,
            pnl_bps=pnl_bps,
            elapsed_ms=elapsed_ns // 1_000_000,
            direction="long" if direction == 1 else "short",
        )

        # Reset state — non-overlapping: cooldown from ENTRY time
        self._state[symbol] = "idle"
        self._next_allowed_ts[symbol] = entry_ts + self._hold_ns
        self._direction[symbol] = 0
