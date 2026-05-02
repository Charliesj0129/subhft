"""TX→TMF Lead-Lag Strategy (R28).

Cross-symbol momentum strategy: large TX volume ticks with price change
predict TMF directional moves at 10-30min horizons.

Signal: TX dvol >= threshold AND dp != 0 → enter TMF in direction of TX dp.
Entry: IOC at TMF best ask (buy) or best bid (sell).
Exit: Fixed horizon (max_hold_ns) OR stop-loss (sl_pts), whichever first.
"""

from __future__ import annotations

from structlog import get_logger

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus
from hft_platform.contracts.strategy import TIF, IntentType, Side
from hft_platform.core import timebase
from hft_platform.events import LOBStatsEvent, TickEvent
from hft_platform.strategy.base import BaseStrategy

logger = get_logger("strategy.tx_tmf_leadlag")

_PTS_SCALE = 10_000
_TERMINAL_ORDER_STATUSES = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.FAILED}

# Session defaults (UTC seconds): 08:45-13:45 TWN = 00:45-05:45 UTC
_DEFAULT_SESSION_START_SEC = 0 * 3600 + 45 * 60
_DEFAULT_SESSION_END_SEC = 5 * 3600 + 45 * 60
_UTC_OFFSET_SEC = 8 * 3600


class _OpenPosition:
    """Tracks a single open TMF position from a lead-lag signal."""

    __slots__ = (
        "entry_ts_ns",
        "entry_price",
        "direction",
        "exit_order_id",
        "awaiting_exit",
        "pending_force_close",
        "aggressive_exit_inflight",
    )

    def __init__(self, entry_ts_ns: int, entry_price: int, direction: int) -> None:
        self.entry_ts_ns = entry_ts_ns
        self.entry_price = entry_price
        self.direction = direction
        self.exit_order_id: str = ""
        self.awaiting_exit: bool = False
        self.pending_force_close: bool = False
        self.aggressive_exit_inflight: bool = False


class TxTmfLeadLagStrategy(BaseStrategy):
    """Cross-symbol lead-lag: TX large ticks predict TMF moves."""

    __slots__ = (
        "_signal_symbol",
        "_trade_symbol",
        "_dvol_threshold",
        "_sl_pts",
        "_max_hold_ns",
        "_max_position_lots",
        "_session_start_sec",
        "_session_end_sec",
        "_utc_offset_sec",
        "_force_close_margin_ns",
        "_last_tx_vol",
        "_last_tx_price",
        "_tx_day",
        "_positions_open",
        "_awaiting_entry",
        "_cooldown_ns",
        "_next_allowed_ts",
    )

    def __init__(
        self,
        strategy_id: str = "tx_tmf_leadlag",
        signal_symbol: str = "TXFD6",
        trade_symbol: str = "TMFD6",
        dvol_threshold: int = 20,
        sl_pts: int = 100,
        max_hold_ns: int = 900_000_000_000,  # 15 min
        max_position_lots: int = 3,
        cooldown_ns: int = 5_000_000_000,  # 5s between entries
        session_start_sec: int = _DEFAULT_SESSION_START_SEC,
        session_end_sec: int = _DEFAULT_SESSION_END_SEC,
        utc_offset_sec: int = _UTC_OFFSET_SEC,
        force_close_margin_ns: int = 30_000_000_000,  # 30s before session end
        **kwargs: object,
    ) -> None:
        super().__init__(
            strategy_id=strategy_id,
            symbols=[signal_symbol, trade_symbol],
            **kwargs,
        )
        self._signal_symbol = str(signal_symbol)
        self._trade_symbol = str(trade_symbol)
        self._dvol_threshold = int(dvol_threshold)
        self._sl_pts = int(sl_pts)
        self._max_hold_ns = int(max_hold_ns)
        self._max_position_lots = int(max_position_lots)
        self._cooldown_ns = int(cooldown_ns)
        self._session_start_sec = int(session_start_sec)
        self._session_end_sec = int(session_end_sec)
        self._utc_offset_sec = int(utc_offset_sec)
        self._force_close_margin_ns = int(force_close_margin_ns)

        # TX volume tracking for dvol computation
        self._last_tx_vol: int = 0
        self._last_tx_price: int = 0
        self._tx_day: int = 0  # day number for reset detection

        # Position management: list of open positions
        self._positions_open: list[_OpenPosition] = []
        # Entry fill tracking: direction of pending entry
        self._awaiting_entry: int = 0  # 0=none, +1=buy pending, -1=sell pending
        self._next_allowed_ts: int = 0

    def _in_session(self, now_ns: int) -> bool:
        # Config thresholds are UTC seconds-of-day; compare in UTC (no offset)
        sec_of_day = (now_ns // 1_000_000_000) % 86400
        start = self._session_start_sec
        end = self._session_end_sec
        if start <= end:
            return start <= sec_of_day <= end
        return sec_of_day >= start or sec_of_day <= end

    def _current_lots(self) -> int:
        return len(self._positions_open) + (1 if self._awaiting_entry != 0 else 0)

    # -----------------------------------------------------------------
    # TX Tick → Signal Detection / TMF Tick → Exit Check
    # -----------------------------------------------------------------
    def on_tick(self, event: TickEvent) -> None:
        # Bug 3 fix: check exits on TMF tick events (not just LOBStats)
        if event.symbol == self._trade_symbol:
            self._check_exits_on_tmf_event(event)
            return

        if event.symbol != self._signal_symbol:
            return

        now_ns = int(event.meta.source_ts or timebase.now_ns())
        price = int(event.price)
        vol = int(event.total_volume) if event.total_volume > 0 else int(event.volume)

        # Day boundary detection: volume resets
        day = now_ns // 86_400_000_000_000
        if day != self._tx_day:
            self._tx_day = day
            self._last_tx_vol = 0
            self._last_tx_price = price

        # Compute dvol
        if self._last_tx_vol == 0:
            self._last_tx_vol = vol
            self._last_tx_price = price
            return

        dvol = vol - self._last_tx_vol
        if dvol < 0:
            # Day boundary in cumulative volume
            dvol = vol
        dp = price - self._last_tx_price

        self._last_tx_vol = vol
        self._last_tx_price = price

        # Signal check
        if dvol < self._dvol_threshold or dp == 0:
            return

        if not self._in_session(now_ns):
            return

        if now_ns < self._next_allowed_ts:
            return

        if self._current_lots() >= self._max_position_lots:
            return

        # Generate entry on TMF
        direction = 1 if dp > 0 else -1
        self._enter_tmf(direction, now_ns)

    def _enter_tmf(self, direction: int, now_ns: int) -> None:
        """Place IOC entry on TMF at best bid/ask."""
        if not self.ctx:
            return

        l1 = self.ctx.get_l1_scaled(self._trade_symbol)
        if l1 is None:
            return

        # l1 = (ts, best_bid, best_ask, mid_x2, spread, bid_depth, ask_depth)
        best_bid = l1[1]
        best_ask = l1[2]

        if direction > 0:
            price = best_ask
            if price <= 0:
                return
            self.buy(self._trade_symbol, price, 1, tif=TIF.IOC)
        else:
            price = best_bid
            if price <= 0:
                return
            self.sell(self._trade_symbol, price, 1, tif=TIF.IOC)

        self._awaiting_entry = direction
        self._next_allowed_ts = now_ns + self._cooldown_ns

        logger.info(
            "leadlag_entry_signal",
            direction="long" if direction > 0 else "short",
            trade_symbol=self._trade_symbol,
            entry_price_pts=price / _PTS_SCALE,
        )

    # -----------------------------------------------------------------
    # TMF Tick/BidAsk → Exit Check (Bug 3 fix)
    # -----------------------------------------------------------------
    def _check_exits_on_tmf_event(self, event: TickEvent) -> None:
        """Check SL / time-kill / EOD exits on any TMF event.

        Bug 3 fix: LOBStats fires infrequently; exit checks must also run
        on TMF Tick events to avoid holding positions beyond max_hold.
        """
        if not self._positions_open:
            return
        if not self.ctx:
            return

        now_ns = int(event.meta.source_ts or timebase.now_ns())

        # Use L1 cache for bid/ask (more accurate than tick price alone)
        l1 = self.ctx.get_l1_scaled(self._trade_symbol)
        if l1 is not None:
            best_bid = l1[1]
            best_ask = l1[2]
        else:
            # Fallback: use tick price as both bid and ask (conservative)
            best_bid = int(event.price)
            best_ask = int(event.price)

        if best_bid <= 0 and best_ask <= 0:
            return

        eod_force = self._near_session_end(now_ns)

        for pos in list(self._positions_open):
            if pos.pending_force_close and not pos.exit_order_id:
                self._emit_aggressive_exit(pos, best_bid, best_ask)
                continue

            if pos.awaiting_exit:
                continue

            entry_price = pos.entry_price
            if entry_price <= 0:
                continue

            mark_price = best_bid if pos.direction > 0 else best_ask
            if mark_price <= 0:
                mark_price = (best_bid + best_ask) // 2
            if mark_price <= 0:
                continue

            pnl_scaled = pos.direction * (mark_price - entry_price)
            elapsed_ns = now_ns - pos.entry_ts_ns

            should_exit = pnl_scaled <= -(self._sl_pts * _PTS_SCALE) or elapsed_ns >= self._max_hold_ns or eod_force
            if not should_exit:
                continue

            if eod_force:
                logger.info(
                    "leadlag_eod_force_close_tick",
                    direction="long" if pos.direction > 0 else "short",
                    entry_price_pts=pos.entry_price / _PTS_SCALE,
                    mark_price_pts=mark_price / _PTS_SCALE,
                )

            if pos.exit_order_id:
                self.cancel(self._trade_symbol, pos.exit_order_id)
                pos.pending_force_close = True
                pos.exit_order_id = ""
                pos.awaiting_exit = False
                continue

            self._emit_aggressive_exit(pos, best_bid, best_ask)

    # -----------------------------------------------------------------
    # TMF LOBStats → SL / Time-Kill Monitoring
    # -----------------------------------------------------------------
    def _near_session_end(self, now_ns: int) -> bool:
        """Return True if current time is within force_close_margin of session end."""
        sec_of_day = (now_ns // 1_000_000_000) % 86400
        margin_sec = self._force_close_margin_ns // 1_000_000_000
        cutoff = self._session_end_sec - margin_sec
        if cutoff < 0:
            cutoff += 86400
        # Handle wrap-around
        if cutoff <= self._session_end_sec:
            return cutoff <= sec_of_day <= self._session_end_sec
        return sec_of_day >= cutoff or sec_of_day <= self._session_end_sec

    def on_stats(self, event: LOBStatsEvent) -> None:
        if event.symbol != self._trade_symbol:
            return
        if not self._positions_open:
            return

        now_ns = int(event.ts or timebase.now_ns())
        best_bid = int(event.best_bid or 0)
        best_ask = int(event.best_ask or 0)

        # EOD force-close: if near session end, close ALL open positions
        eod_force = self._near_session_end(now_ns)

        # Check each open position for exit conditions
        for pos in list(self._positions_open):
            if pos.pending_force_close and not pos.exit_order_id:
                self._emit_aggressive_exit(pos, best_bid, best_ask)
                continue

            if pos.awaiting_exit:
                continue

            entry_price = pos.entry_price
            if entry_price <= 0:
                continue

            # Mark-to-market using opposing side
            mark_price = best_bid if pos.direction > 0 else best_ask
            if mark_price <= 0:
                mark_price = (best_bid + best_ask) // 2
            if mark_price <= 0:
                continue

            pnl_scaled = pos.direction * (mark_price - entry_price)
            elapsed_ns = now_ns - pos.entry_ts_ns

            should_exit = pnl_scaled <= -(self._sl_pts * _PTS_SCALE) or elapsed_ns >= self._max_hold_ns or eod_force
            if not should_exit:
                continue

            if eod_force:
                logger.info(
                    "leadlag_eod_force_close",
                    direction="long" if pos.direction > 0 else "short",
                    entry_price_pts=pos.entry_price / _PTS_SCALE,
                    mark_price_pts=mark_price / _PTS_SCALE,
                )

            # Cancel resting exit if exists
            if pos.exit_order_id:
                self.cancel(self._trade_symbol, pos.exit_order_id)
                pos.pending_force_close = True
                pos.exit_order_id = ""
                pos.awaiting_exit = False
                # Do NOT clear aggressive_exit_inflight here — the cancel
                # hasn't been ACK'd yet.  Clearing it prematurely allows a
                # duplicate FORCE_FLAT IOC if the next tick re-enters
                # _force_close_all before the cancel ACK arrives.  The
                # on_order() terminal-status handler (line ~500) resets the
                # flag safely after the broker confirms the cancellation.
                continue

            self._emit_aggressive_exit(pos, best_bid, best_ask)

    def _emit_aggressive_exit(self, pos: _OpenPosition, best_bid: int, best_ask: int) -> None:
        if pos.aggressive_exit_inflight:
            return
        if pos.direction > 0:
            side, price = Side.SELL, best_bid
        else:
            side, price = Side.BUY, best_ask
        if price <= 0:
            return
        if self.ctx:
            intent = self.ctx.place_order(
                symbol=self._trade_symbol,
                side=side,
                price=price,
                qty=1,
                tif=TIF.IOC,
                intent_type=IntentType.FORCE_FLAT,
            )
            self._generated_intents.append(intent)
        pos.aggressive_exit_inflight = True
        pos.awaiting_exit = True
        pos.pending_force_close = False

    # -----------------------------------------------------------------
    # Fill / Order Handling
    # -----------------------------------------------------------------
    def on_fill(self, event: FillEvent) -> None:
        if event.symbol != self._trade_symbol:
            return

        # Entry fill
        if self._awaiting_entry != 0:
            entry_side = Side.BUY if self._awaiting_entry > 0 else Side.SELL
            if event.side == entry_side:
                pos = _OpenPosition(
                    entry_ts_ns=int(event.match_ts_ns or timebase.now_ns()),
                    entry_price=int(event.price),
                    direction=self._awaiting_entry,
                )
                self._positions_open.append(pos)
                self._awaiting_entry = 0
                logger.info(
                    "leadlag_entry_filled",
                    direction="long" if pos.direction > 0 else "short",
                    price_pts=pos.entry_price / _PTS_SCALE,
                    open_positions=len(self._positions_open),
                )
                return

        # Exit fill — match by order_id first, then awaiting_exit flag, then side
        # Priority: exact order_id match > inflight exit with no order_id yet > side-only
        best_match = None
        for pos in self._positions_open:
            exit_side = Side.SELL if pos.direction > 0 else Side.BUY
            if event.side != exit_side:
                continue
            if pos.exit_order_id and pos.exit_order_id == event.order_id:
                best_match = pos
                break  # exact match — use immediately
            if pos.aggressive_exit_inflight and not pos.exit_order_id and best_match is None:
                best_match = pos  # inflight but order_id not yet set — best guess
            elif not best_match:
                best_match = pos  # fallback: side-only
        if best_match is not None:
            pos = best_match
            pnl_pts = pos.direction * (int(event.price) - pos.entry_price) // _PTS_SCALE
            self._positions_open.remove(pos)
            logger.info(
                "leadlag_exit_filled",
                pnl_pts=pnl_pts,
                open_positions=len(self._positions_open),
            )
            return

    def on_order(self, event: OrderEvent) -> None:
        if event.symbol != self._trade_symbol:
            return

        # Entry order rejection/cancellation
        if self._awaiting_entry != 0:
            entry_side = Side.BUY if self._awaiting_entry > 0 else Side.SELL
            if event.side == entry_side and event.status in _TERMINAL_ORDER_STATUSES and event.filled_qty == 0:
                self._awaiting_entry = 0
                return

        # Exit order tracking
        for pos in self._positions_open:
            exit_side = Side.SELL if pos.direction > 0 else Side.BUY
            if event.side != exit_side:
                continue

            if pos.awaiting_exit:
                if event.status in {
                    OrderStatus.SUBMITTED,
                    OrderStatus.PARTIALLY_FILLED,
                    OrderStatus.PENDING_SUBMIT,
                }:
                    pos.exit_order_id = event.order_id
                    pos.awaiting_exit = False
                    return
                if event.status in _TERMINAL_ORDER_STATUSES and event.filled_qty == 0:
                    pos.aggressive_exit_inflight = False
                    pos.awaiting_exit = False
                    pos.pending_force_close = True
                    return

            if event.order_id == pos.exit_order_id:
                if event.status == OrderStatus.FILLED:
                    self._positions_open.remove(pos)
                    return
                if event.status in _TERMINAL_ORDER_STATUSES:
                    pos.exit_order_id = ""
                    pos.awaiting_exit = False
                    pos.aggressive_exit_inflight = False
                    pos.pending_force_close = True
                return
